#!/usr/bin/env python3
"""Postit de e-mails: últimos 5 e-mails com resumo, em popup estilo postit.

Busca via Gmail IMAP (credenciais.env do leitormail), resume cada e-mail com
LLM local (ollama/llama3.1, com fallback para prévia do corpo) e mostra um
postit amarelo acima de todas as janelas que fecha sozinho em ~30 s.

--dry  imprime os e-mails/resumos em texto (nenhuma janela).
"""
import email
import imaplib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime
from pathlib import Path

HOME = Path.home()
BASE = HOME / ".config/omarchy-voice/plugins/leitormail"
ENV = BASE / "credenciais.env"
SYS_PATH = str(BASE)
if SYS_PATH not in sys.path:
    sys.path.insert(0, SYS_PATH)

from leitormail import (  # noqa: E402
    CAT_AZUL,
    CAT_AMARELO,
    CAT_VERDE,
    CORES_EMPREGO,
    DATA_MIN,
    body_text,
    classificar_emprego,
    dec_hdr,
    e_data_ok,
    e_relevante,
    load_env,
)

NUM = 5
SCAN = 30  # janela de e-mails mais recentes conferidos
MODEL = os.environ.get("MAIL_POSTIT_MODEL", "llama3.1")
# Se MAIL_POSTIT_LLM=1, tenta resumo via ollama (pode demorar em CPU).
# Sem ele, o postit aparece imediato com a prévia limpa de cada e-mail.
LLM = bool(os.environ.get("MAIL_POSTIT_LLM"))
SUMMARY_TIMEOUT = 30
BODY_MAX = 200
PREVIEW = 600  # conteúdo mostrado em cada postit
NOTE_CM_W = 7.0   # largura do postit em centímetros (pedido do usuário)
NOTE_CM_H = 10.0  # altura do postit em centímetros
NOTE_SECONDS = 30
YELLOW = CORES_EMPREGO[CAT_AMARELO]["cor"]
BORDER = CORES_EMPREGO[CAT_AMARELO]["borda"]
DARK = CORES_EMPREGO[CAT_AMARELO]["tinta"]

LOG = HOME / ".local/state/omarchy-voice/postit.log"


def log(msg):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(f"{datetime.now().isoformat()} {msg}\n")


def fetch_emails(addr, pwd, num=NUM):
    """Retorna e-mails RELEVANTES (vaga/proposta/entrevista de emprego) dos
    últimos SCAN e-mails, até `num`."""
    mails = []
    fora_data = 0
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=30)
        mail.login(addr, pwd)
        mail.select("INBOX")
        typ, data = mail.search(None, "ALL")
        if typ != "OK":
            mail.logout()
            return mails
        ids = [int(x) for x in data[0].split()][-SCAN:]
        for uid in ids:
            res, msgdata = mail.fetch(str(uid), "(RFC822)")
            if res != "OK":
                continue
            raw = b"\r\n".join(b[1] for b in msgdata if isinstance(b, tuple))
            msg = email.message_from_bytes(raw)
            subj = dec_hdr(msg.get("Subject")) or "(sem assunto)"
            rem = dec_hdr(msg.get("From")) or "desconhecido"
            trecho = re.sub(r"\s+", " ", body_text(msg)).strip()
            if not e_data_ok(msg):
                fora_data += 1
                continue
            if not e_relevante(subj, rem, trecho, addr):
                continue
            cat, cor, rotulo = classificar_emprego(subj, rem, trecho)
            mails.append({"subject": subj, "from": rem,
                          "cat": cat, "cor": cor,
                          "snippet": trecho[:160], "preview": trecho[:PREVIEW],
                          "body": trecho, "url": url_for(msg, subj)})
            if len(mails) >= num:
                break
        mail.logout()
        extra = f", {fora_data} de antes de {DATA_MIN:%Y-%m} descartados" if fora_data else ""
        log(f"fetch ok: {len(mails)} relevantes de emprego (janela {len(ids)}{extra})")
    except Exception as e:
        log(f"ERRO fetch: {e}")
        mails = []
    return mails


def url_for(msg, subj):
    """Link direto para o e-mail em si no Gmail (via Message-ID quando existe).

    O deep-link por Message-ID abre o e-mail EXATO na caixa de entrada; só
    funciona no navegador em que a conta está logada. Sem Message-ID, cai numa
    busca restrita por remetente + assunto completo (que é o próprio e-mail)."""
    from urllib.parse import quote
    mid = msg.get("Message-ID")
    if mid:
        mid_clean = mid.strip().strip("<>").strip()
        if mid_clean:
            return f"https://mail.google.com/mail/u/0/#all/rfc822msgid:{quote(mid_clean, safe='@._-:%=')}"
    rem = (msg.get("From") or "").strip()
    q = quote(f'from:{rem} subject:"{subj}"')
    return f"https://mail.google.com/mail/u/0/#search/{q}"


def summarize(mails):
    """Resumo em uma chamada ollama (API nativa); fallback = prévia do corpo."""
    if not mails:
        return []
    prompt = (
        f"Você resume e-mails para um usuário brasileiro. Seguem {len(mails)} "
        "e-mails de VAGA DE EMPREGO/PROPOSTA/ENTREVISTA no formato "
        "'### N\nDe: ...\nAssunto: ...\nCorpo: ...'. Para CADA um escreva UMA "
        "linha curta (máx. 100 caracteres, em português) que diga o assunto "
        "essencial e a ação esperada (responder, enviar currículo, agendar...). "
        f"Responda APENAS com {len(mails)} linhas numeradas 1..{len(mails)}, "
        "sem títulos.\n\n"
    )
    for i, m in enumerate(mails, 1):
        prompt += (f"### {i}\nDe: {m['from']}\nAssunto: {m['subject']}"
                   f"\nCorpo: {m['body'][:BODY_MAX]}\n")
    host = os.environ.get("OLLAMA_HOST", "127.0.0.1:11434")
    payload = json.dumps({
        "model": MODEL, "prompt": prompt, "stream": False,
        "options": {"temperature": 0.3, "num_ctx": 1024, "num_predict": 50},
    }).encode()
    try:
        req = urllib.request.Request(
            f"http://{host}/api/generate", data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=SUMMARY_TIMEOUT) as r:
            out = json.loads(r.read().decode()).get("response", "")
        if not out.strip():
            raise RuntimeError("resposta vazia")
        num = {}
        for ln in out.splitlines():
            m = re.match(r"^(\d{1,2})[.:-]?\s*(.*)$", ln.strip())
            if m:
                num[int(m.group(1))] = m.group(2).strip()
        log(f"resumo ollama ok ({len(num)}/{len(mails)})")
        return [num.get(i, mails[i - 1]["snippet"].strip())
                for i in range(1, len(mails) + 1)]
    except Exception as e:
        log(f"ERRO resumo (fallback prévia): {e}")
        return [m["snippet"].strip() or "(sem corpo)" for m in mails]


def summarize_sync(mails):
    """Versão com barra de progresso mostrada no próprio postit (fallback do dry)."""
    return summarize(mails)


def gen_gtk(tmp_path, items):
    """Gera um script Gtk independente que desenha UM postit por e-mail
    (7cm x 10cm): assunto em cima + resumo do conteúdo embaixo, rolável,
    com X de fechar no canto e cor por categoria (azul/amarelo/verde)."""
    script = f'''#!/usr/bin/env python3
import gi, json, os, random, subprocess, time
import re
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GLib, Pango, cairo

DEFAULT_COR = "{YELLOW}"
DEFAULT_BORDA = "{BORDER}"
DEFAULT_TINTA = "{DARK}"
NOTE_SECONDS = {NOTE_SECONDS}
CM_W = {NOTE_CM_W}
CM_H = {NOTE_CM_H}
CLOSE_SZ = 24

def get_screen():
    try:
        mons = json.loads(subprocess.run(
            ["hyprctl", "monitors", "-j"], capture_output=True, text=True).stdout)
    except Exception:
        return 2400, 1350, 0, 0, 1210, 1.0
    for m in mons:
        if m.get("focused"):
            return (int(m["width"]), int(m["height"]), int(m["x"]), int(m["y"]),
                    int(m.get("physicalWidth") or 1210), float(m.get("scale") or 1.0))
    return 2400, 1350, 0, 0, 1210, 1.0

SW, SH, OX, OY, PMM, S = get_screen()
LSW = int(SW / S)  # espaço lógico do Hyprland (layout px: 3840/1.6 = 2400)
LSH = int(SH / S)
PX_PER_CM = LSW / (PMM / 10.0)
W = max(100, int(CM_W * PX_PER_CM))
H = max(150, int(CM_H * PX_PER_CM))

items = {json.dumps(items, ensure_ascii=False)}

def hex_to_rgb(s):
    s = str(s or "").lstrip("#")
    if len(s) != 6:
        s = "fef89a"
    return tuple(int(s[i:i + 2], 16) / 255.0 for i in (0, 2, 4))

def hypr(lua):
    subprocess.run(["hyprctl", "dispatch", lua],
                   capture_output=True, text=True, check=False)

def find_addr_now(title):
    try:
        out = subprocess.run(["hyprctl", "clients", "-j"],
                             capture_output=True, text=True).stdout
        for c in json.loads(out):
            if (c.get("title") or "").startswith(title):
                return c["address"]
    except Exception:
        pass
    return ""

def settle(win, title, px, py, n=0):
    addr = find_addr_now(title)
    if not addr:
        if n < 40:
            GLib.timeout_add(120, lambda: settle(win, title, px, py, n + 1))
        return
    # Resize primeiro (o Hyprland recentraliza ao redimensionar) e move depois.
    hypr('hl.dsp.window.resize({{ window = "address:' + addr +
         '", x = ' + str(W) + ', y = ' + str(H) + ' }})')
    hypr('hl.dsp.window.move({{ window = "address:' + addr +
         '", x = ' + str(px) + ', y = ' + str(py) + ' }})')
    if n < 2:
        GLib.timeout_add(150, lambda: settle(win, title, px, py, n + 1))

placed = []
def random_pos(i):
    margx = 30; margy_t = 70; margy_b = 40
    Wp, Hp = int(W), int(H)
    gx = 14; gy = 14
    for _ in range(60):
        px = int(random.randint(OX + margx, OX + LSW - Wp - margx))
        py = int(random.randint(OY + margy_t, OY + LSH - Hp - margy_b))
        ok = True
        for (ox, oy) in placed:
            if abs(px - ox) < Wp + gx and abs(py - oy) < Hp + gy:
                ok = False
                break
        if ok:
            placed.append((px, py))
            return px, py
    px = int(OX + LSW - Wp - margx)
    py = int(OY + margy_t + (i - 1) * (Hp + gy))
    placed.append((px, py))
    return px, py

def esc(t):
    return GLib.markup_escape_text(str(t), -1)

def round_rect(cr, x, y, w, h, r):
    cr.new_path()
    cr.arc(x + r, y + r, r, 3.14159, 1.5 * 3.14159)
    cr.arc(x + w - r, y + r, r, 1.5 * 3.14159, 2 * 3.14159)
    cr.arc(x + w - r, y + h - r, r, 0, 0.5 * 3.14159)
    cr.arc(x + r, y + h - r, r, 0.5 * 3.14159, 3.14159)
    cr.close_path()

def note_colors(widget):
    c = getattr(widget, "_cor", None)
    if not c:
        c = {{"cor": DEFAULT_COR, "borda": DEFAULT_BORDA, "tinta": DEFAULT_TINTA}}
    return c.get("cor", DEFAULT_COR), c.get("borda", DEFAULT_BORDA), c.get("tinta", DEFAULT_TINTA)

def draw_close(cr, w, h, tinta):
    x0 = w - CLOSE_SZ - 6
    y0 = 6
    cr.set_source_rgba(0, 0, 0, 0.13)
    round_rect(cr, x0, y0, CLOSE_SZ, CLOSE_SZ, 6); cr.fill()
    cr.set_source_rgb(*hex_to_rgb(tinta))
    cr.set_line_width(1.8)
    cr.set_line_cap(cairo.LineCap.ROUND)
    m = CLOSE_SZ * 0.27
    cr.move_to(x0 + m, y0 + m); cr.line_to(x0 + CLOSE_SZ - m, y0 + CLOSE_SZ - m)
    cr.move_to(x0 + CLOSE_SZ - m, y0 + m); cr.line_to(x0 + m, y0 + CLOSE_SZ - m)
    cr.stroke()

def on_draw(widget, cr):
    cr.set_source_rgba(0, 0, 0, 0); cr.paint()
    alloc = widget.get_allocation(); w, h = alloc.width, alloc.height; rx = 9.0
    cor, borda, tinta = note_colors(widget)
    cr.set_source_rgba(0, 0, 0, 0.18)
    round_rect(cr, 4, 4, w, h, rx); cr.fill()
    cr.set_source_rgb(*hex_to_rgb(cor))
    round_rect(cr, 0, 0, w, h, rx); cr.fill()
    cr.set_source_rgb(*hex_to_rgb(borda))
    cr.set_line_width(1.6)
    round_rect(cr, 1, 1, w - 2, h - 2, rx - 1); cr.stroke()
    draw_close(cr, w, h, tinta)
    return False

def tick(win):
    win._left -= 1
    if win._left <= 0:
        win.destroy()
    return win._left > 0

def open_and_close(win, lnk):
    if lnk:
        subprocess.Popen(["xdg-open", lnk])
    win._left = 0
    win.destroy()

def add_close(win, link):
    def on_click(w, ev):
        ww = w.get_allocated_width()
        if ev.x >= ww - CLOSE_SZ - 10 and ev.y <= CLOSE_SZ + 10:
            w._left = 0
            w.destroy()
            return True
        open_and_close(w, link)
        return True
    win.connect("button-press-event", on_click)

def make_note(i, it, px, py):
    title = f"mail_postit_omarchy_{{i}}_{{os.getpid()}}"
    link = str(it.get("url") or "")
    cor = it.get("cor") or {{}}
    tinta = cor.get("tinta", DEFAULT_TINTA)
    var = "\u200b"
    win = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
    win.set_title(title)
    win.set_wmclass("mail_postit_omarchy", "mail_postit_omarchy")
    win.set_decorated(False)
    win.set_skip_taskbar_hint(True)
    win.set_skip_pager_hint(True)
    win.set_keep_above(True)
    win.set_accept_focus(True)
    win.set_visual(win.get_screen().get_rgba_visual())
    win.set_app_paintable(True)
    win._cor = cor
    win.connect("draw", on_draw)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    box.set_border_width(7)
    subj = Gtk.Label()
    if link:
        subj.set_markup(f'<span font_family="Noto Sans Bold" size="x-small" foreground="#0b5394" underline="single">{{esc(it["subject"])}}</span>')
    else:
        subj.set_markup(f'<span font_family="Noto Sans Bold" size="x-small" color="{{tinta}}">{{esc(it["subject"])}}</span>')
    subj.set_line_wrap(True)
    subj.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
    subj.set_xalign(0); subj.set_yalign(0)
    subj.set_max_width_chars(24)
    box.pack_start(subj, False, False, 0)
    sep = Gtk.Separator()
    sep.set_margin_top(3); sep.set_margin_bottom(3)
    box.pack_start(sep, False, False, 0)
    body = Gtk.Label()
    texto = str(it.get("sum") or it.get("preview") or it.get("snippet") or "(sem conteúdo)")
    texto = re.sub(r"([/@%_#&?=])", var + r"\1" + var, texto)  # permite quebrar tokens longos
    body.set_markup(f'<span size="small" color="{{tinta}}">{{esc(texto)}}</span>')
    body.set_line_wrap(True)
    body.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
    body.set_xalign(0); body.set_yalign(0)
    body.set_max_width_chars(24)
    box.pack_start(body, True, True, 0)
    sw = Gtk.ScrolledWindow()
    sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
    sw.set_min_content_width(int(W * 0.86))
    sw.set_min_content_height(int(H * 0.6))
    sw.add(box)
    win.add(sw)
    add_close(win, link)
    win._left = NOTE_SECONDS
    GLib.timeout_add(1000, lambda: tick(win))
    win.set_resizable(False)
    win.set_default_size(W, H)
    win.resize(W, H)
    win.show_all()
    win.set_type_hint(Gdk.WindowTypeHint.UTILITY)
    win.present()
    GLib.timeout_add(150, lambda: settle(win, title, px, py, 0))
    return win

for i, it in enumerate(items, 1):
    px, py = random_pos(i)
    make_note(i, it, px, py)
Gtk.main()
'''
    tmp_path.write_text(script)
    tmp_path.chmod(0o755)


def show_popup(items):
    """Desenha os postits em um processo Gtk separado, matando após NOTE_SECONDS."""
    tmp = Path(tempfile.gettempdir()) / f"mail_postit_tmp_{os.getpid()}.py"
    gen_gtk(tmp, items)
    try:
        proc = subprocess.Popen([sys.executable, str(tmp)])
        try:
            proc.wait(timeout=NOTE_SECONDS + 5)
        except subprocess.TimeoutExpired:
            proc.kill()
    finally:
        tmp.unlink(missing_ok=True)


def main():
    dry = "--dry" in sys.argv
    env = load_env()
    addr = env.get("GMAIL_ADDR", "")
    pwd = env.get("GMAIL_PASS", "")
    if not addr or not pwd:
        print("sem credencial", file=sys.stderr)
        return 1
    mails = fetch_emails(addr, pwd)
    if not mails:
        print("nenhum e-mail relevante (vaga/entrevista)", file=sys.stderr)
        if not dry:
            subprocess.run(["notify-send", "-a", "MailPostit",
                            "Sem e-mails de vaga/entrevista no momento"],
                           check=False)
        return 0
    sums = summarize(mails) if LLM else [m["preview"].strip() for m in mails]
    for m, s in zip(mails, sums):
        m["sum"] = s
    if dry:
        for i, m in enumerate(mails, 1):
            print(f"{i}. {m['subject']}")
            print(f"   de {m['from']}")
            print(f"   {m['sum']}")
        return 0
    show_popup(mails)
    return 0


if __name__ == "__main__":
    sys.exit(main())