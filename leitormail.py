#!/usr/bin/env python3
import base64
import email
import html
import imaplib
import os
import quopri
import re
import subprocess
import sys
from datetime import datetime
from email.header import decode_header
from email.utils import parsedate_to_datetime
from pathlib import Path

HOME = Path.home()
BASE = HOME / ".config/omarchy-voice/plugins/leitormail"
ENV = BASE / "credenciais.env"
STATE = HOME / ".local/state/omarchy-voice/leitormail_uid"
LOG = HOME / ".local/state/omarchy-voice/leitormail.log"
BIRD = BASE / "passarinho.sh"
MAX = 3
# Só entram e-mails a partir desta data: vaga de ano passado (2023/2024/2025)
# ou de antes de setembro/2026 não interessa mais. A janela do IMAP já
# pegava mensagem antiga e o filtro de relevância não olhava a data.
DATA_MIN = datetime(2026, 9, 1)
# Post-it automático: desligado a pedido do dono (2026-09-29) — o timer
# repetia as mesmas 5 vagas na tela a cada 16 min. Análise continua no
# relatório diário; o post-it manual (omarchy-mail-postit) continua existindo.
POSTIT_AUTO = False


def e_data_ok(msg, minimo=DATA_MIN):
    """True se o e-mail é de `minimo` em diante (padrão: 2026-09-01).

    E-mail sem `Date` legível é aceito (não se descarta por falta de data),
    porque o que filtra mesmo é a janela do IMAP e a relevância do texto.
    """
    bruto = msg.get("Date")
    if not bruto:
        return True
    try:
        d = parsedate_to_datetime(bruto)
    except (TypeError, ValueError):
        return True
    if d is None:
        return True
    if d.tzinfo is not None:
        d = d.astimezone().replace(tzinfo=None)  # pro fuso local, comparável
    return d >= minimo


def load_env():
    data = {}
    for line in ENV.read_text().splitlines():
        if line and "=" in line:
            k, v = line.split("=", 1)
            data[k.strip()] = v.strip()
    return data


OSD_PLUGIN = os.environ.get("LEITORMAIL_OSD_PLUGIN", "omarchy.osd")


def summon_bird():
    """Dispara o passarinho voador no overlay (acima de fullscreen, com som).

    O OSD é um PanelWindow na camada WlrLayer.Overlay, ou seja a única
    janela que Hyprland desenha por cima de qualquer tela cheia. O IpcHandler
    do clone toca o sino (canberra service-login) quando o payload traz
    sound:"bird" e o open() liga o voo com trilha de estrelinhas.
    """
    payload = json.dumps({"sound": "bird", "duration": "5600"})
    subprocess.run(
        [
            "quickshell",
            # A barra do omarchy sobe com `-p /usr/share/omarchy/shell`; sem o
            # mesmo -p o summon externo não encontra o shell.qml e cai exit 255.
            "-p",
            "/usr/share/omarchy/shell",
            "ipc",
            "call",
            "shell",
            "summon",
            OSD_PLUGIN,
            payload,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=str(HOME),
    )


def dec_hdr(value):
    if not value:
        return ""
    out = ""
    try:
        for text, enc in decode_header(value):
            if isinstance(text, bytes):
                out += text.decode(enc or "utf-8", "replace")
            else:
                out += text
    except Exception:
        out = str(value)
    return out


# Só vagas/propostas/entrevistas de EMPREGO passam. O resto é descartado.
_EMPREGO_POS = [
    "vaga", "vagas", "emprego", "empregos", "oportunidade",
    "recrutador", "recrutamento", "entrevista", "entrevistas",
    "processo seletivo", "candidatura", "candidato", "candidata",
    "contratando", "contratação", "headhunter", "busca ativa",
    "hiring", "job", "job offer", "job vacancy", "recruiter",
    "position", "interview", "careers", "career", "huntress",
    "gupy", "vagas.com", "linkedin", "trabalhe conosco",
    "carreira", "carreiras", "curriculo", "currículo", "seletiva",
    "desenvolvedor", "programador", "analista", "engenheir", "estag",
    "remoto", "presencial", "salari", "vaga para", "empresa contrata",
    "proposta de emprego", "proposta de trabalho", "oferta de emprego",
]
_EMPREGO_NEG = [
    "cupom", "cumpom", "promo", "desconto", "liquidacao",
    "assine", "assinatura", "fatura", "boleto", "parceria comercial",
    "proposta comercial", "campanha", "atualize seus dados",
    "compre", "comprar", "cashback", "milhõ", "oferta limitada",
    "pague", "pagamento", "extrato", "banc",
    "newsletter", "digest", "news", "carteira", "investe",
    "investimento", "ações", "renda fixa", "ibovespa", "selic", "cripto",
]
_EMPREGO_NO = [  # assunto com cara de promoção/evento → nunca é vaga
    "black friday", "friday", "condição especial", "condicao especial",
    "sai do ar", "antecipad", "imperdí", "imperd", "últimos dias",
    "ultimos dias", "esgotar", "últimas horas", "ultimas horas",
    "é amanhã", "e amanha", "forma especial",
    "promo", "desconto", "cupom", "agora por",
]


def e_relevante(subj, rem, corpo, addr=""):
    """True se o e-mail fala de VAGA/PROPOSTA/ENTREVISTA de emprego."""
    subj = subj or ""
    rem = rem or ""
    if addr and addr.lower() in rem.lower():
        return False  # mensagem da própria conta (ex.: relatório do SysMon)
    suj = subj.lower()
    if any(k in suj for k in _EMPREGO_NO):
        return False  # promo/evento no assunto → ignora
    in_subject = any(p in suj for p in _EMPREGO_POS)
    if "newsletter" in rem.lower() or "digest" in rem.lower():
        return in_subject  # boletim só passa se o assunto for explicitamente de vaga
    t = " ".join((subj, rem, corpo or "")).lower()
    hits = {p for p in _EMPREGO_POS if p in t}
    negs = {n for n in _EMPREGO_NEG if n in t}
    if not hits:
        return False
    if in_subject:
        return True  # palavra forte no assunto → vaga/entrevista
    if len(negs) >= 2 and len(hits) <= 2:
        return False  # cara de comércio/marketing, não de emprego
    return len(hits) >= 2


# ---- Classificação por categoria/cor dos post-its --------------------------
# azul = entrevista ou proposta (maior importância); amarelo = acompanhamento
# de candidatura/processo; verde = vaga nova para se candidatar.
CAT_AZUL = 3
CAT_AMARELO = 2
CAT_VERDE = 1

CORES_EMPREGO = {
    CAT_AZUL: {
        "rotulo": "entrevista/proposta", "cor": "#bfe4ff", "borda": "#3a97d6",
        "tinta": "#0b2c44",
    },
    CAT_AMARELO: {
        "rotulo": "acompanhamento", "cor": "#fef39a", "borda": "#e0c000",
        "tinta": "#4a3b00",
    },
    CAT_VERDE: {
        "rotulo": "vaga", "cor": "#cff7c5", "borda": "#5bb24f",
        "tinta": "#1f3d1a",
    },
}

_ENTREVISTA = [
    "entrevista", "interview", "agenda", "agendar", "agendada", "agendado",
    "reunião", "reuniao", "meeting", "chamada", "video chamada", "videochamada",
    "videoconferência", "conversa", "conversar", "convite", "calendário",
    "calendario", "calendar", "horário", "horario", "confirmar disponibilidade",
    "rapid", "bate-papo", "vamos conversar", "zoom", "hangout", "2a etapa",
    "2º etapa", "segunda etapa", "próxima etapa", "proxima etapa",
]
_PROPOSTA = [
    "proposta", "job offer", "oferta de emprego", "oferta", "selecionado",
    "aprovado", "você passou", "temos uma proposta", "contrato",
    "pretensão salarial", "você foi selecionado", "vamos contratar",
    "queremos você", "ramo de contratação",
]
_ACOMPANHAMENTO = [
    "candidatura", "inscrição", "inscricao", "sua candidatura",
    "candidatura recebida", "recebemos sua", "recebemos a sua",
    "em análise", "em analise", "em avaliação", "avaliação", "avaliacao",
    "andamento", "status", "atualização", "atualizacao", "etapa", "fase",
    "pré-selecionad", "pre-selecionad", "teste", "teste técnico",
    "desafio técnico", "desafio", "agora é com você", "aguardando",
    "resultado", "feedback", "prazo", "validação", "validacao",
    "segue o andamento", "dando sequência", "sequência", "sequencia",
    "próximos passos", "proximos passos", "analisando", "analisando seu",
    "continuamos", "devolutiva",
]
_VAGA = [
    "vaga", "vagas", "oportunidade", "oportunidades", "contratando",
    "estamos contratando", "hiring", "job", "position", "career", "careers",
    "carreira", "carreiras", "trabalhe conosco", "gupy", "vaga aberta",
    "nova vaga", "vaga disponível", "vaga disponivel", "precisamos de",
    "oportunidade para", "employs", "processo seletivo aberto", "linkedin",
]


def classificar_emprego(subj, rem, corpo):
    """Classifica um e-mail relevante em entrevista/proposta (azul),
    acompanhamento (amarelo) ou vaga nova (verde).

    Ordem de precedência: entrevista > proposta > acompanhamento > vaga.
    Devolve a tupla (categoria, dict de cores, rótulo)."""
    t = " ".join(str(x or "") for x in (subj, rem, corpo)).lower()
    for nome, pats in (("entrevista", _ENTREVISTA), ("proposta", _PROPOSTA),
                       ("acompanhamento", _ACOMPANHAMENTO), ("vaga", _VAGA)):
        if any(p in t for p in pats):
            cat = {"entrevista": CAT_AZUL, "proposta": CAT_AZUL,
                   "acompanhamento": CAT_AMARELO, "vaga": CAT_VERDE}[nome]
            cor = CORES_EMPREGO[cat]
            return cat, cor, cor["rotulo"]
    cor = CORES_EMPREGO[CAT_VERDE]
    return CAT_VERDE, cor, cor["rotulo"]


def _clean_text(raw, _depth=0):
    """Transforma um corpo CRU em texto legível.

    Lida com os 4 casos que o Gmail entrega sem cabeçalho MIME no topo:
      1. HTML já decodificado → tira tags/entidades;
      2. base64 puro → decodifica e reprocessa;
      3. quoted-printable (ex.: `=3D`) → decodifica e reprocessa;
      4. MIME aninhado (bloco `--boundary`/`=_Part_...` dentro de texto/plain)
         → reparseia e extrai o melhor texto.
    """
    def _looks_b64(s):
        t = re.sub(r"\s+", "", s)
        if len(t) < 40 or re.fullmatch(r"[A-Za-z0-9+/]*={0,4}", t) is None:
            return False
        # evita decodificar "texto de letras soltas" como se fosse base64
        try:
            dec = base64.b64decode(t + "=" * (-len(t) % 4))
            probe = dec.decode("ascii", "replace")
        except Exception:
            return False
        return " " in probe or "\n" in probe or "\r" in probe

    def _has_qp(s):
        return re.search(r"=[0-9A-Fa-f]{2}", s) is not None

    def _looks_mime(s):
        head = s[:2000]
        has_hdr = re.search(r"(?im)^(Content-Type|Mime-Version|Content-Transfer-Encoding)\s*:", head)
        has_bnd = bool(re.match(r"-{2,}\w+\r?\n", s)) or "_Part_" in head or "boundary" in head[:500]
        return bool(has_hdr or has_bnd)

    def _mime_text(blob, _depth=0):
        if _depth > 4:
            return ""
        m = email.message_from_string(blob)
        best = ""
        for part in m.walk():
            ct = part.get_content_type()
            if ct == "message/rfc822":
                inner = part.get_payload()
                if inner:
                    best = best or _mime_text(inner.as_string() if hasattr(inner, "as_string") else str(inner), _depth + 1)
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                txt = payload.decode(charset, "replace")
            except (LookupError, ValueError):
                txt = payload.decode("utf-8", "replace")
            txt = _clean_text(txt, _depth + 1)
            t = re.sub(r"\s+", " ", txt).strip()
            if t:
                if ct == "text/plain":
                    return t  # ama texto puro
                best = best or t
        return best

    def _scrub_mime(s):
        """Remove restos de MIME (cabeçalhos/limites) que sobraram no texto."""
        linhas = re.split(r"\r?\n", s)
        limpas = []
        for ln in linhas:
            l = ln.strip()
            if not l:
                limpas.append("")
                continue
            if re.match(r"^-{2,}\w+", l) or re.match(r"^Content-[\w-]+\s*:", l, re.I):
                continue
            if re.match(r"^(Mime-Version|Content-ID|Content-Location|Received|X-[\w-]+)\s*:", l, re.I):
                continue
            limpas.append(l)
        return re.sub(r"[ \t]+", " ", "\n".join(limpas)).strip()

    if not raw:
        return ""

    s = raw if isinstance(raw, str) else raw.decode("utf-8", "replace")

    if _depth > 5:
        return _scrub_mime(s)

    if _looks_b64(s):
        try:
            dec = base64.b64decode(re.sub(r"\s+", "", s) + "=" * (-len(re.sub(r"\s+", "", s)) % 4))
            txt = _clean_text(dec.decode("utf-8", "replace"), _depth + 1)
            if txt:
                return txt
        except Exception:
            pass

    if _looks_mime(s):
        got = _mime_text(s, _depth)
        if got:
            return got

    if _has_qp(s):
        try:
            dec = quopri.decodestring(s.encode("utf-8", "replace")).decode("utf-8", "replace")
            if dec != s:
                return _clean_text(dec, _depth + 1)
        except Exception:
            pass

    if "<" in s and ">" in s and re.search(r"<[a-zA-Z/][a-zA-Z0-9 !=\"'/.-]*>", s):
        # sobra de apresentação não é texto: joga fora style/script antes
        s = re.sub(r"(?is)<(style|script|head)[^>]*>.*?</\1>", " ", s)
        s = html.unescape(s)
        # tag de bloco vira quebra de linha; o resto vira espaço
        s = re.sub(r"(?i)<(br|/p|/div|/li|/tr|/h[1-6])(\s[^>]*)?>", "\n", s)
        s = re.sub(r"<[^>]+>", " ", s)
        return re.sub(r"[ \t]+", " ", s).strip()

    return re.sub(r"\s+", " ", s).strip()


def body_text(msg):
    """Extrai o corpo em texto COESO e LEGÍVEL, mesmo de e-mails mal-formados.

    Muitos remetentes enviam o corpo como MIME cru dentro de um part único
    `text/plain` sem Content-Transfer-Encoding — o `get_payload(decode=True)`
    devolve isso cru e o resultado vira "um monte de caracteres sem sentido".
    Aqui cada candidato passa pelo `_clean_text` (base64/HTML/QP/MIME) e o
    texto que fizer mais sentido é devolvido.
    """
    def _textos_candidatos():
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() in ("text/plain", "text/html"):
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or "utf-8"
                        try:
                            yield payload.decode(charset, "replace")
                        except (LookupError, ValueError):
                            yield payload.decode("utf-8", "replace")
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                raw = payload
                charset = msg.get_content_charset() or "utf-8"
                try:
                    yield raw.decode(charset, "replace")
                except (LookupError, ValueError):
                    yield raw.decode("utf-8", "replace")

    melhores = []
    for cand in _textos_candidatos():
        txt = _clean_text(cand)
        t = re.sub(r"\s+", " ", txt).strip()
        if t:
            melhores.append(t)
    if not melhores:
        return ""
    # quanto mais espaços (e menos "lixo") o texto tem, mais legível ele é
    return max(melhores, key=lambda t: (len(re.findall(r"\S{2,}", t)), -t.count("="), len(t)))


def notify(nova, subj, remetente, anexo, trecho):
    bird = ""
    if nova:
        bird = " \\n 🐦 novo"
    has_anexo = "\\n 📎 tem anexo (não li)" if anexo else ""
    body = f"{remetente}\\n{trecho}{has_anexo}{bird}"
    subprocess.run(
        ["notify-send", "-a", "LeitorMail", subj[:80], body[:400]],
        check=False,
    )


def main():
    env = load_env()
    addr = env.get("GMAIL_ADDR", "")
    pwd = env.get("GMAIL_PASS", "")
    if not addr or not pwd:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        LOG.write_text("sem credencial\n")
        return
    STATE.parent.mkdir(parents=True, exist_ok=True)
    last = 0
    if STATE.exists():
        try:
            last = int(STATE.read_text().strip())
        except ValueError:
            last = 0
    novos = []
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=30)
        mail.login(addr, pwd)
        mail.select("INBOX")
        typ, data = mail.search(None, "ALL")
        if typ != "OK":
            mail.logout()
            return
        ids = [int(x) for x in data[0].split()]
        if not ids:
            mail.logout()
            return
        janela = ids[-30:]
        fora_data = 0
        for uid in janela:
            if uid <= last:
                continue
            res, msgdata = mail.fetch(str(uid), "(RFC822)")
            if res != "OK":
                continue
            raw = b"\r\n".join(b[1] for b in msgdata if isinstance(b, tuple))
            msg = email.message_from_bytes(raw)
            subj = dec_hdr(msg.get("Subject")) or "(sem assunto)"
            rem = dec_hdr(msg.get("From")) or "desconhecido"
            anexo = any(part.get_filename() for part in msg.walk() if part.get_filename())
            corpo = re.sub(r"\s+", " ", body_text(msg)).strip()
            if not e_data_ok(msg):
                fora_data += 1
                continue
            if not e_relevante(subj, rem, corpo, addr):
                continue
            novos.append((subj, rem, anexo, corpo[:260]))
        mail.logout()
    except Exception as e:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        LOG.write_text(f"{datetime.now().isoformat()} ERRO: {e}\n")
        return
    alta = max(janela) if janela else 0
    if alta > 0:
        STATE.write_text(str(alta))
    if not novos:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        extra = f", {fora_data} de antes de {DATA_MIN:%Y-%m} descartados" if fora_data else ""
        LOG.write_text(f"{datetime.now().isoformat()} OK 0 relevantes "
                       f"(de {len(janela)} novos conferidos{extra})\n")
        return
    LOG.parent.mkdir(parents=True, exist_ok=True)
    extra = f", {fora_data} de antes de {DATA_MIN:%Y-%m} descartados" if fora_data else ""
    LOG.write_text(f"{datetime.now().isoformat()} OK {len(novos)} relevantes de emprego{extra}\n")
    first = True
    for subj, rem, anexo, trecho in novos[-MAX:]:
        notify(first, subj, rem, anexo, trecho)
        first = False
    if not POSTIT_AUTO:
        return
    subprocess.Popen(
        ["systemd-run", "--user", "--no-block", "--collect", "--unit=leitormail-postit",
         sys.executable, str(BASE / "postit.py")],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


if __name__ == "__main__":
    main()
