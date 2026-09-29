# LeitorMail — post-its de e-mail na tela

Post-its virtuais que aparecem **por cima da tela** com as vagas e propostas de
emprego que chegaram no Gmail, e um relatório kanban diário por e-mail.

Roda em background via `systemd` de usuário, custa quase nada de CPU e não tem
processo de janela permanente.

![linux desktop](https://img.shields.io/badge/Linux-Hyprland-informational) ![gtk](https://img.shields.io/badge/GTK3-blue)

## Como funciona

```
Gmail (IMAP)  ──▶  leitormail.py     timer de 16 min, filtra e notifica
                        │
                        ├─▶ postit.py        desenha os post-its GTK na tela
                        └─▶ relatorio_vagas.py   kanban HTML por e-mail (10h/18h)
```

1. `leitormail.py` roda a cada 16 min e só olha os UIDs **novos** (o último
   lido fica em `leitormail_uid`).
2. `e_relevante()` deixa passar só vaga/proposta/entrevista de emprego — ignora
   `self`, promoções e newsletter genérica.
3. `e_data_ok()` descarta o que é anterior a `DATA_MIN` (2026-09-01).
4. `classificar_emprego()` decide a cor:
   **azul** = entrevista/proposta · **amarelo** = acompanhamento · **verde** = vaga nova.
5. `postit.py` desenha as notas flutuando em posição aleatória sem sobreposição,
   fechando sozinhas em ~30 s (ou no X). O assunto é link direto para o e-mail
   no Gmail.
6. O relatório manda um kanban HTML (Entrevistas/Propostas · Vagas ·
   Acompanhamento) às 10h e 18h.

## Instalação

```bash
cp credenciais.env.example credenciais.env
chmod 600 credenciais.env
$EDITOR credenciais.env          # e-mail + senha de app do Gmail

mkdir -p ~/.config/omarchy-voice/plugins/leitormail
cp leitormail.py postit.py relatorio_vagas.py alternar.sh passarinho.sh \
   ~/.config/omarchy-voice/plugins/leitormail/
mkdir -p ~/.local/state/omarchy-voice

cp leitormail.service leitormail.timer \
   leitormail-relatorio.service leitormail-relatorio.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now leitormail.timer
systemctl --user enable  --now leitormail-relatorio.timer
```

## Hyprland (para os post-its aparecerem sobre tela cheia)

Os post-its só aparecem sobre tela cheia se o Hyprland souber que eles são uma
camada de overlay:

```hyprland
windowrulev2 = float, class:^mail_postit.*, fullscreen, alpha 0.95
```

O matcher é **full-match** por *classe*: a classe GTK vira do nome do script
temporário, e por isso o padrão precisa da âncora `^`.

Em tela 4K com `scale 1.6`, converta cm para **px lógicos** (`LSW = SW / scale`)
antes de posicionar.

## Barra / widget

`alternar.sh` imprime JSON no formato waybar e implementa o liga/desliga:

```bash
./alternar.sh            # estado em JSON (waybar)
./alternar.sh estado     # "on" / "off"
./alternar.sh toggle     # liga/desliga o timer
```

## Comandos manuais

```bash
./omarchy-mail-toggle    # liga/desliga a automação
./omarchy-mail-postit    # mostra os post-its na hora
```

Instale-os em `~/.local/bin/` (ligue o `omarchy-mail-toggle` a um atalho, ex. `Super+F12`):

```bash
cp omarchy-mail-toggle omarchy-mail-postit ~/.local/bin/
```

## Variáveis

| Onde | Chave | Padrão | Para quê |
|---|---|---|---|
| `credenciais.env` | `GMAIL_ADDR` / `GMAIL_PASS` | — | IMAP (senha de **app**, não a senha do Google) |
| `credenciais.env` | `INTERVALO_MIN` / `MAX_EMAILS` | 16 / 3 | ritmo e teto por rodada |
| `leitormail.py` | `DATA_MIN` | `2026-09-01` | descarta e-mail anterior a esta data |
| `leitormail.py` | `POSTIT_AUTO` | `False` | post-it automático desligado; só o manual |
| ambiente | `MAIL_POSTIT_LLM` | `0` | resumo via ollama (lento em CPU) |
| ambiente | `LEITORMAIL_OSD_PLUGIN` | `omarchy.osd` | plugin Quickshell do "passarinho" |

## Detalhes que custaram tempo (e voltam para quem chegar depois)

- **`subprocess.Popen` não sobrevive a um timer do systemd.** O filho fica no
  cgroup do serviço `Type=oneshot`, e `KillMode=control-group` mata o grupo todo
  quando o timer termina. O serviço "rodava", o log confirmava, e o post-it nunca
  aparecia. A correção é rodar o post-it como **unidade transiente**:
  `systemd-run --user --no-block --collect --unit=leitormail-postit python3 postit.py`.
- **Relevância sem data é mentira.** Um digest de dezembro
  de 2023 era tão "relevante" quanto o de hoje. O filtro olhava só o texto; o
  `e_data_ok()` passou a comparar o header `Date` (e-mail sem `Date` legível
  passa). Aplicado nos **três** pontos de entrada: monitor, post-it e relatório.
- **`hl.dsp.window.resize` recentraliza a janela** — por isso o resize vem antes
  do move, nunca depois.
- **Filtro de relevância sem memória repete a mesma vaga a cada rodada.** O
  `fetch_emails` relê a janela do IMAP do zero, então sem um UID/lista de "já
  vistos" o monitor reenche a tela com as mesmas 5 vagas. Aqui a decisão foi
  desligar o post-it automático e deixar a consolidação para o relatório diário.

## Requisitos

- Python 3 com **PyGObject/GTK 3** (só o `postit.py` usa GUI)
- `hyprctl` no PATH
- Linux com systemd (unidades de usuário)
- senha de app do Gmail para IMAP
