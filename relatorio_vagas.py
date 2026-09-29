#!/usr/bin/env python3
"""LeitorMail — relatório diário de e-mails de trabalho com kanban.

Reagrupa os e-mails relevantes de trabalho (entrevista/proposta · vaga ·
acompanhamento) das últimas horas e envia um e-mail HTML com kanban,
segregado por categoria. O relatório é enviado DA própria conta, então o
sistema de post-its o ignora (e_relevante descarta mensagens da própria
conta) — cumprindo o requisito de não virar post-it.

--dry  apenas imprime a contagem e o HTML (não envia).
"""
import email
import imaplib
import re
import smtplib
import sys
from datetime import datetime
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid
from pathlib import Path

HOME = Path.home()
BASE = HOME / ".config" / "omarchy-voice" / "plugins" / "leitormail"
ENV = BASE / "credenciais.env"
LOG = HOME / ".local" / "state" / "omarchy-voice" / "relatorio_vagas.log"
STATE_DIR = HOME / ".local" / "state" / "omarchy-voice"

sys.path.insert(0, str(BASE))
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
from postit import url_for  # noqa: E402  (só funções; não abre janela)

JANELA = 150  # últimos e-mails conferidos para o relatório
MAX_POR_CAT = 30  # limite por coluna do kanban


def log(msg):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now().isoformat()} {msg}\n")


def fetch_lote(addr, pwd, janela=JANELA):
    """Todos os e-mails relevantes de trabalho da janela (sem limite)."""
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
        ids = [int(x) for x in data[0].split()][-janela:]
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
            mails.append({"subject": subj, "from": rem, "cat": cat,
                          "cor": cor, "rotulo": rotulo,
                          "snippet": trecho[:180], "url": url_for(msg, subj)})
        mail.logout()
        extra = f", {fora_data} de antes de {DATA_MIN:%Y-%m} descartados" if fora_data else ""
        log(f"fetch ok: {len(mails)} relevantes de trabalho (janela {len(ids)}{extra})")
    except Exception as e:
        log(f"ERRO fetch: {e}")
        mails = []
    return mails


def esc_html(t):
    return (str(t or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def card_html(m):
    subj = esc_html(m["subject"])
    rem = esc_html(m["from"].split("<")[-1].rstrip(">") if "<" in m["from"] else m["from"])
    snip = esc_html(m["snippet"])
    url = m.get("url") or ""
    href = f'<a href="{url}" style="text-decoration:none;">' if url else ""
    hsec = "</a>" if url else ""
    return (
        f'<div style="background:#ffffff;border-radius:8px;padding:8px;'
        f'margin:0 0 8px 0;border:1px solid #e3e3e3;">'
        f'{href}<div style="font-size:13px;font-weight:bold;color:#17303f;">'
        f'{subj}</div></a>'
        f'<div style="font-size:11px;color:#6b7280;margin-top:2px;">{rem} · '
        f'<span style="color:{esc_html(m["cor"]["borda"])};font-weight:bold;">'
        f'{esc_html(m["rotulo"])}</span></div>'
        f'<div style="font-size:12px;color:#374151;margin-top:4px;">{snip}</div>'
        f'</div>'
    )


def kanban_html(por_cat):
    ordem = [(CAT_AZUL, "Entrevistas / Propostas"),
             (CAT_VERDE, "Vagas novas"),
             (CAT_AMARELO, "Acompanhamento")]
    cols = []
    for cat, titulo in ordem:
        cor = CORES_EMPREGO[cat]
        its = por_cat.get(cat, [])
        cards = "".join(card_html(m) for m in its) or (
            '<div style="color:#9ca3af;font-size:12px;padding:6px;">'
            'Nenhum e-mail hoje.</div>')
        cols.append(
            f'<td style="width:33%;vertical-align:top;padding:6px;'
            f'background:{cor["cor"]};border-radius:10px;">'
            f'<div style="font-size:12px;font-weight:bold;color:{cor["tinta"]};'
            f'padding:6px;border-bottom:2px solid {cor["borda"]};'
            f'margin-bottom:8px;">{titulo} '
            f'<span style="opacity:.75;">({len(its)})</span></div>{cards}</td>')
    return f'<table style="width:100%;border-collapse:separate;border-spacing:6px;">' \
           f'<tr>{"".join(cols)}</tr></table>'


def build_html(total, por_cat):
    now = datetime.now().strftime("%d/%m/%Y %H:%M")
    resumo = " · ".join(
        f'{CORES_EMPREGO[cat]["rotulo"]}: {len(por_cat.get(cat, []))}'
        for cat in (CAT_AZUL, CAT_VERDE, CAT_AMARELO))
    return f"""<!DOCTYPE html><html><body style="font-family:'Segoe UI',sans-serif;background:#f0f1f4;padding:16px;">
<div style="max-width:720px;margin:0 auto;">
<div style="background:#ffffff;border-radius:12px;padding:16px;margin-bottom:12px;">
<h2 style="margin:0 0 4px 0;color:#17303f;">LeitorMail · Resumo do dia</h2>
<div style="color:#6b7280;font-size:13px;">{now} · {total} e-mails de trabalho</div>
<div style="color:#374151;font-size:13px;margin-top:6px;">{resumo}</div>
</div>
{kanban_html(por_cat)}
<div style="color:#9ca3af;font-size:11px;margin-top:12px;text-align:center;">
Relatório automático LeitorMail (omarchy-voice). O sistema de post-its ignora esta mensagem.</div>
</div></body></html>"""


def texto_plano(total, por_cat):
    linhas = [f"LeitorMail — resumo do dia ({datetime.now():%d/%m/%Y %H:%M})",
              f"Total de e-mails de trabalho: {total}", ""]
    for cat, nome in ((CAT_AZUL, "ENTREVISTAS / PROPOSTAS"),
                      (CAT_VERDE, "VAGAS NOVAS"),
                      (CAT_AMARELO, "ACOMPANHAMENTO")):
        linhas.append(f"[{nome}]")
        for m in por_cat.get(cat, []):
            linhas.append(f"  - {m['subject']} ({m['from']})")
        linhas.append("")
    return "\n".join(linhas)


def send_mail(addr, pwd, html, txt, subject):
    msg = MIMEText(html, "html", "utf-8")
    msg["Subject"] = subject
    msg["From"] = addr
    msg["To"] = addr
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="PCLEY")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=40) as s:
        s.login(addr, pwd)
        s.sendmail(addr, [addr], msg.as_string())


def main():
    dry = "--dry" in sys.argv
    env = load_env()
    addr = env.get("GMAIL_ADDR", "")
    pwd = env.get("GMAIL_PASS", "")
    if not addr or not pwd:
        print("sem credencial", file=sys.stderr)
        return 1
    mails = fetch_lote(addr, pwd)
    por_cat = {}
    for m in mails:
        por_cat.setdefault(m["cat"], []).append(m)
    for cat in por_cat:
        por_cat[cat] = por_cat[cat][:MAX_POR_CAT]
    total = len(mails)
    subject = f"[LeitorMail] Resumo de trabalho {datetime.now():%d/%m} ({total})"
    txt = texto_plano(total, por_cat)
    html = build_html(total, por_cat)
    print(txt)
    print(f"\nenviando -> {addr} | {subject}")
    if dry:
        print("(modo --dry, não enviou)")
        return 0
    send_mail(addr, pwd, html, txt, subject)
    log("e-mail enviado")
    return 0


if __name__ == "__main__":
    sys.exit(main())