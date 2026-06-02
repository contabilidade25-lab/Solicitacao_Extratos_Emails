import base64
import html
import os
import re
import sys
from datetime import datetime
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import anthropic
import pytz
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# ╔══════════════════════════════════════════════════════════╗
# ║                     CONFIGURAÇÕES                        ║
# ╚══════════════════════════════════════════════════════════╝

EMAIL_SAMEA   = "contabilidade36@akartos.com.br"
EMAIL_GERENTE = "contabilidade16@akartos.com.br"
EMAIL_CEO     = "kasseus@akartos.com.br"

CLAUDE_API_KEY = "SUA_CHAVE_AQUI"  # cole sua chave de https://console.anthropic.com

TIMEZONE = pytz.timezone("America/Manaus")

PLANILHA_CLIENTES_ID = "1gZUGKOk0VXw1KOb3Shx3MVQez53bGb1jvZCUHEheCeA"
PLANILHA_SITUACAO_ID = "1gerzpnG_vaXIXWxBxwE4kJ8k5kfUWauLhjG1_Q2nnNM"
GOOGLE_DOC_SIMULACAO = "1wQTEnv-d68qe6yU7H8XYF3C6fqIV-Lmz94jPzdZEj3c"

# ASSINATURA_IMG definida abaixo após _BASE (compatível com PyInstaller)

# Nome EXATO da aba de clientes
ABA_CLIENTES = "SOLICITAÇÕES - RASCUNHO"

# Colunas (0-based)
COL_NOME   = 0   # A — Nome da empresa
COL_EMAIL  = 1   # B — E-mail real do cliente
COL_BANCOS = 5   # F — Bancos a serem solicitados
COL_TESTE  = 9   # J — E-mail de TESTE (filtro principal!)
COL_IGNORAR = 12 # M — Se tiver "IGNORAR", pula o cliente

# "teste" → envia para coluna J | "real" → envia para coluna B
MODO = "real"

# DATA_INICIO: clientes sem nenhuma thread após esta data são cobrados normalmente.
# Atualizado para 01/06/2026 — todos os clientes da planilha entram no ciclo,
# inclusive os que nunca receberam solicitação automática antes.
DATA_INICIO = "01/06/2026"

# Histórico lido para contexto vai até esta data (inclusive threads mais antigas)
# Threads antes desta data são ignoradas completamente
DATA_HISTORICO = "06/03/2026"

# E-mails internos da AKARTOS — respostas deles não contam como resposta do cliente
EMAILS_INTERNOS = {
    "contabilidade36@akartos.com.br",  # Sâmea
    "contabilidade16@akartos.com.br",  # Gestora
    "kasseus@akartos.com.br",          # CEO
}

# Padrões de remetente que devem ser ignorados completamente (notificações automáticas)
_REMETENTES_IGNORAR_RE = re.compile(
    r"mailer-daemon|postmaster|noreply|no-reply|delivery.{0,10}subsystem"
    r"|mail.{0,5}delivery|bounce|auto.{0,5}reply|notificacao|notification",
    re.IGNORECASE,
)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/documents",
]

if getattr(sys, "frozen", False):       # rodando como .exe (PyInstaller)
    _BASE = os.path.dirname(sys.executable)
else:                                   # rodando como .py normal
    _BASE = os.path.dirname(os.path.abspath(__file__))

TOKEN_PATH     = os.path.join(_BASE, "token.json")
CREDS_PATH     = os.path.join(_BASE, "credentials.json")
ASSINATURA_IMG = os.path.join(_BASE, "ramals", "ramal_samea.png")

# ╔══════════════════════════════════════════════════════════╗
# ║              MESES + REGEX DO EMOJI 📌                   ║
# ╚══════════════════════════════════════════════════════════╝

MESES_PT = {
    1:"JANEIRO", 2:"FEVEREIRO", 3:"MARÇO",   4:"ABRIL",
    5:"MAIO",    6:"JUNHO",     7:"JULHO",    8:"AGOSTO",
    9:"SETEMBRO",10:"OUTUBRO", 11:"NOVEMBRO", 12:"DEZEMBRO",
}
MES_CANONICO = {}
for _num, _nome in MESES_PT.items():
    MES_CANONICO[_nome] = _nome
    MES_CANONICO[_nome[:3]] = _nome

EMOJI_MES_RE = re.compile(
    r"📌\s*\*?"
    r"(janeiro|fevereiro|março|marco|abril|maio|junho|julho|"
    r"agosto|setembro|outubro|novembro|dezembro|"
    r"jan|fev|mar|abr|mai|jun|jul|ago|set|out|nov|dez)"
    r"\*?"
    r"(?:[/\-]?\s*(\d{2,4}))?",
    re.IGNORECASE | re.UNICODE,
)

# Regex para extrair meses de texto livre (sem 📌) — usado em mensagens de correção
MES_LIVRE_RE = re.compile(
    r"\b(janeiro|fevereiro|março|marco|abril|maio|junho|julho|"
    r"agosto|setembro|outubro|novembro|dezembro|"
    r"jan|fev|mar|abr|mai|jun|jul|ago|set|out|nov|dez)\b",
    re.IGNORECASE | re.UNICODE,
)


def extrair_meses_livres(texto: str) -> list:
    if not texto:
        return []
    encontrados, vistos = [], set()
    for match in MES_LIVRE_RE.finditer(texto):
        mes = _norm_mes(match.group(1))
        if mes not in vistos:
            vistos.add(mes)
            encontrados.append(mes)
    return encontrados


def _norm_mes(m: str) -> str:
    m = m.upper().replace("MARCO", "MARÇO")
    if len(m) <= 3:
        return MES_CANONICO.get(m, m)
    return m


def extrair_meses_marcados(texto: str) -> list:
    if not texto:
        return []
    encontrados, vistos = [], set()
    for match in EMOJI_MES_RE.finditer(texto):
        mes = _norm_mes(match.group(1))
        if mes not in vistos:
            vistos.add(mes)
            encontrados.append(mes)
    return encontrados

# ╔══════════════════════════════════════════════════════════╗
# ║             ASSUNTOS — VÁLIDOS E IGNORADOS               ║
# ╚══════════════════════════════════════════════════════════╝

ASSUNTO_VALIDO_RE = re.compile(
    r"(SOLICI[TT]A[ÇC][ÃA]O\s+DE\s+EXTRATOS\s+BANC[ÁA]RIOS"
    r"|📌\s*Solicita[çc][ãa]o\s+de\s+Extrato"
    r"|Re:\s*|RES:\s*)",
    re.IGNORECASE,
)

ASSUNTOS_IGNORAR = [
    "Fw: Seu extrato está disponível",
    "NOTIFICAÇÃO","NFS-e","NOTA FISCAL",
    "Recibo de Adiantamento","Delivery Status Notification",
    "Lidas:","propaganda","oferta",
]

# Marca onde começa o boilerplate nos e-mails da Sâmea — tudo após isso é descartado pro chatbot
_BOILERPLATE_SAMEA_RE = re.compile(
    r"Quaisquer necessidades conte comigo"
    r"|Peço a gentileza"
    r"|As informa[çc][õo]es contidas nesta mensagem"
    r"|The information contained in this message"
    r"|O envio mensal dos extratos banc[áa]rios [eé] fundamental"
    r"|Fico no aguardo do seu retorno",
    re.IGNORECASE,
)


def _limpar_corpo_para_chatbot(corpo: str, eh_samea: bool) -> str:
    if not corpo:
        return ""
    if eh_samea:
        m = _BOILERPLATE_SAMEA_RE.search(corpo)
        if m:
            corpo = corpo[:m.start()].strip()
    return re.sub(r"\n{3,}", "\n\n", corpo).strip()


# Frases que indicam que o cliente DISSE que estava enviando um arquivo
# (usado para detectar "disse que enviou mas não veio anexo")
_CLIENTE_DISSE_ENVIOU_RE = re.compile(
    r"segue[s]?\s+(em\s+)?anexo"
    r"|em\s+anexo\s+segue"
    r"|segue\s+(o|os|a|as)\s+(extrato|documento|arquivo|comprovante|pdf|ofx|demonstrativo)"
    r"|estou\s+(enviando|encaminhando|mandando)"
    r"|enc[ao]minho|encaminhamos"
    r"|enviamos|enviei|envio\s+em\s+anexo"
    r"|conforme\s+solicitad[ao]"
    r"|favor\s+(encontrar|verificar)\s+em\s+anexo"
    r"|vide\s+anexo|veja\s+(o\s+)?anexo"
    r"|segue\s+conforme"
    r"|anexo\s+(segue|estou|enviando)"
    r"|estou\s+anexando|segue\s+o\s+arquivo",
    re.IGNORECASE | re.UNICODE,
)

# ╔══════════════════════════════════════════════════════════╗
# ║                    AUTENTICAÇÃO GOOGLE                   ║
# ╚══════════════════════════════════════════════════════════╝

def get_credentials():
    creds = None
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CREDS_PATH):
                raise FileNotFoundError(
                    f"\n❌ credentials.json não encontrado em: {CREDS_PATH}\n"
                )
            flow  = InstalledAppFlow.from_client_secrets_file(CREDS_PATH, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_PATH, "w") as f:
            f.write(creds.to_json())
    return creds

# ╔══════════════════════════════════════════════════════════╗
# ║                  PLANILHAS GOOGLE SHEETS                 ║
# ╚══════════════════════════════════════════════════════════╝

def listar_clientes(creds):
    """
    Lê a aba SOLICITAÇÕES - RASCUNHO e retorna todos os
    clientes com e-mail na coluna B (email real) e
    que NÃO têm "IGNORAR" na coluna M.
    """
    service = build("sheets", "v4", credentials=creds)
    range_  = f"'{ABA_CLIENTES}'!A:M"  # Lê até coluna M
    res = service.spreadsheets().values().get(
        spreadsheetId=PLANILHA_CLIENTES_ID, range=range_
    ).execute()

    linhas    = res.get("values", [])
    clientes  = []
    sem_email = []

    for i, linha in enumerate(linhas):
        cel = lambda idx: linha[idx].strip() if idx < len(linha) else ""

        nome        = cel(COL_NOME)     # Coluna A
        email_real  = cel(COL_EMAIL)    # Coluna B
        bancos      = cel(COL_BANCOS)   # Coluna F
        ignorar     = cel(COL_IGNORAR)  # Coluna M

        # ⭐ SE TIVER "IGNORAR" NA COLUNA M, PULA COMPLETAMENTE ⭐
        if ignorar.upper() == "IGNORAR":
            print(f"   ⏭️ Cliente IGNORADO (coluna M): {nome}")
            continue

        # Defesa extra: nome não pode estar vazio
        if not nome:
            continue

        email_destino = email_real
        if "@" not in email_destino:
            if nome:
                sem_email.append({"nome": nome, "linha_planilha": i + 1})
            continue

        clientes.append({
            "linha_planilha": i + 1,
            "nome":           nome,
            "email_real":     email_real,
            "email_destino":  email_destino,
            "bancos":         [b.strip() for b in bancos.split(",") if b.strip()],
        })

    return clientes, sem_email


def atualizar_situacao(creds, linha: int, status: str):
    service = build("sheets", "v4", credentials=creds)
    agora   = datetime.now(TIMEZONE).strftime("%d/%m/%Y %H:%M")
    body    = {"values": [[str(linha), status, agora]]}
    service.spreadsheets().values().append(
        spreadsheetId=PLANILHA_SITUACAO_ID,
        range="A:C",
        valueInputOption="USER_ENTERED",
        body=body,
    ).execute()
    print(f"    ✅ Situação atualizada: linha {linha} → {status}")

# ╔══════════════════════════════════════════════════════════╗
# ║                  GMAIL — LEITURA + PARSE                 ║
# ╚══════════════════════════════════════════════════════════╝

def _header(msg, nome):
    for h in msg.get("payload", {}).get("headers", []):
        if h["name"].lower() == nome.lower():
            return h["value"]
    return ""


_MIME_IMAGEM = {
    "image/png", "image/jpeg", "image/jpg", "image/gif",
    "image/bmp", "image/webp", "image/svg+xml", "image/tiff",
}

_MIME_ASSINATURA_DIGITAL = {
    "application/pkcs7-signature",
    "application/x-pkcs7-signature",
    "application/pkcs7-mime",
    "application/x-pkcs7-mime",
}

_FILENAME_ASSINATURA_RE = re.compile(r"^smime\.p7[smb]$", re.IGNORECASE)

def _tem_anexo(msg):
    def _buscar(parts):
        for p in parts:
            filename = p.get("filename", "").strip()
            if filename:
                hdrs = {h["name"].lower(): h["value"].lower()
                        for h in p.get("headers", [])}
                disp = hdrs.get("content-disposition", "")
                mime = p.get("mimeType", "").lower()

                # Arquivo embutido no corpo (assinatura/logo de qualquer formato):
                # tem Content-ID (cid:...) OU Content-Disposition: inline
                tem_cid    = "content-id" in hdrs
                eh_inline  = disp.startswith("inline")
                if tem_cid or eh_inline:
                    pass  # ignora — é parte do corpo, não extrato

                # Assinatura digital S/MIME (smime.p7s, etc.)
                elif mime in _MIME_ASSINATURA_DIGITAL or _FILENAME_ASSINATURA_RE.match(filename):
                    pass

                else:
                    return True  # arquivo real anexado

            if _buscar(p.get("parts", [])):
                return True
        return False
    return _buscar(msg.get("payload", {}).get("parts", []))


def _decode_part(part):
    data = part.get("body", {}).get("data", "")
    if not data:
        return ""
    try:
        return base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
    except Exception:
        return ""


def _extrair_corpo_completo(msg) -> str:
    plain, html = [], []

    def percorrer(part):
        mime = part.get("mimeType", "")
        if mime == "text/plain":
            plain.append(_decode_part(part))
        elif mime == "text/html":
            html.append(_decode_part(part))
        for sub in part.get("parts", []):
            percorrer(sub)

    percorrer(msg.get("payload", {}))

    if plain:
        texto = "\n".join(plain)
        # Remove linhas citadas (>) e linha de atribuição ("Em dd de mai. de 2026, X escreveu:")
        linhas = [l for l in texto.splitlines() if not l.lstrip().startswith(">")]
        texto = "\n".join(linhas)
        texto = re.sub(r"\bEm\b[^\n]{0,200}\bescreveu\b[^\n]*", "", texto, flags=re.IGNORECASE)
        return texto
    if html:
        h = "\n".join(html)
        # Remove blocos de citação (Gmail blockquote e div.gmail_quote)
        h = re.sub(r"<blockquote[^>]*>[\s\S]*?</blockquote>", "", h, flags=re.IGNORECASE)
        h = re.sub(r'<div[^>]*class=["\'][^"\']*gmail_quote[^"\']*["\'][^>]*>[\s\S]*?</div\s*>', "", h, flags=re.IGNORECASE)
        # Gmail armazena emoji como <img data-emoji="📌" alt="📌"> — restaura o char antes de strip
        h = re.sub(r'<img[^>]+data-emoji="([^"]+)"[^>]*/?>', r'\1', h, flags=re.IGNORECASE)
        h = re.sub(r"<br\s*/?>", "\n", h, flags=re.IGNORECASE)
        h = re.sub(r"</p>", "\n", h, flags=re.IGNORECASE)
        h = re.sub(r"<[^>]+>", "", h)
        h = h.replace("&nbsp;", " ").replace("&amp;", "&")
        h = h.replace("&lt;", "<").replace("&gt;", ">")
        # Remove linha de atribuição de citação após strip do HTML
        # ex: "Em sex., 22 de mai. de 2026 às 13:25, X escreveu:"
        h = re.sub(r"\bEm\b[^\n]{0,200}\bescreveu\b[^\n]*", "", h, flags=re.IGNORECASE)
        return h
    return ""


def _assunto_ignorado(assunto: str) -> bool:
    a = assunto.lower()
    return any(ig.lower() in a for ig in ASSUNTOS_IGNORAR)


def _assunto_valido(assunto: str) -> bool:
    return (not _assunto_ignorado(assunto)) and bool(ASSUNTO_VALIDO_RE.search(assunto))


def buscar_threads_cliente(creds, email_cliente: str):
    service = build("gmail", "v1", credentials=creds)
    query   = f"(from:{email_cliente} OR to:{email_cliente})"
    res = service.users().threads().list(
        userId="me", q=query, maxResults=50
    ).execute()
    threads = []
    for t in res.get("threads", []):
        try:
            thread = service.users().threads().get(
                userId="me", id=t["id"], format="full"
            ).execute()
        except TypeError:
            # Fallback para versões antigas da lib que usam threadId
            thread = service.users().threads().get(
                userId="me", threadId=t["id"], format="full"
            ).execute()
        msgs = thread.get("messages", [])
        if not msgs:
            continue
        if not _assunto_valido(_header(msgs[0], "Subject")):
            continue
        threads.append({
            "thread_id": t["id"],
            "assunto":   _header(msgs[0], "Subject"),
            "mensagens": msgs,
        })
    return threads


def analisar_thread(thread: dict) -> dict:
    historico = []
    _ts_historico = (
        datetime.strptime(DATA_HISTORICO, "%d/%m/%Y").replace(tzinfo=TIMEZONE).timestamp() * 1000
        if DATA_HISTORICO else 0
    )
    msgs_ordenadas = sorted(thread["mensagens"], key=lambda m: int(m.get("internalDate", 0)))
    for msg in msgs_ordenadas:
        # Ignora mensagens anteriores a DATA_HISTORICO
        if int(msg.get("internalDate", 0)) < _ts_historico:
            continue
        de    = _header(msg, "From")
        # Ignora notificações automáticas (mailer-daemon, bounce, noreply...)
        if _REMETENTES_IGNORAR_RE.search(de):
            continue
        corpo = _extrair_corpo_completo(msg)
        # eh_interno: Sâmea, gestora ou CEO — não conta como resposta do cliente
        eh_interno = any(e in de.lower() for e in EMAILS_INTERNOS)
        eh_samea   = EMAIL_SAMEA.lower() in de.lower()
        meses_marcados = extrair_meses_marcados(corpo) if eh_samea else []
        historico.append({
            "de":             de,
            "corpo":          corpo,
            "tem_anexo":      _tem_anexo(msg),
            "eh_samea":       eh_samea,
            "eh_interno":     eh_interno,
            "meses_marcados": meses_marcados,
            "timestamp":      int(msg.get("internalDate", 0)),
        })
    samea_iniciou     = historico[0]["eh_samea"] if historico else False
    # Só conta como resposta do cliente mensagens de pessoas externas (não internas)
    cliente_respondeu = any((not h["eh_interno"]) for h in historico[1:])
    tem_anexo_cliente = any(h["tem_anexo"] for h in historico[1:] if not h["eh_interno"])
    ultima_de         = historico[-1]["de"] if historico else ""
    # tem_anexo_ultima: última mensagem do CLIENTE (externo) tem anexo
    ultima_msg_cliente = next((h for h in reversed(historico[1:]) if not h["eh_interno"]), None)
    tem_anexo_ultima   = ultima_msg_cliente["tem_anexo"] if ultima_msg_cliente else False

    # samea_corrigiu: última msg da Sâmea contém frase de correção manual
    _FRASES_CORRECAO = [
        "identificamos que ainda permanece pendente",
        "identificamos que ainda permanecem pendentes",
        "não corresponde ao banco",
        "identificamos que o anexo",
        "não identificamos entre os anexo",
        "não identificamos entre os anexos",
    ]
    samea_corrigiu  = False
    meses_correcao  = []
    if cliente_respondeu:
        for h in reversed(historico[1:]):
            if h["eh_samea"]:
                corpo_s = h["corpo"] or ""
                cl = corpo_s.lower()
                samea_corrigiu = any(f in cl for f in _FRASES_CORRECAO)
                if samea_corrigiu:
                    meses_correcao = extrair_meses_livres(corpo_s)
                break

    # samea_ja_agradeceu: Sâmea já confirmou recebimento em algum momento
    # MAS só fecha o caso se o cliente NÃO voltou a falar depois desse agradecimento
    _FRASES_AGRADECIMENTO = [
        "muito obrigada pelo envio", "obrigada pelo envio",
        "obrigado pelo envio", "vamos processar", "documentos recebidos",
        "extratos recebidos", "recebemos os extratos", "recebemos o extrato",
        "já recebemos", "já recebi", "passarão por análise", "passará por análise",
        "recebi sim", "recebi os extratos", "recebi o extrato",
        # frases mais amplas que o chatbot pode gerar
        "tudo certinho", "certinho por aqui", "recebi tudo",
        "recebido com sucesso", "confirmo o recebimento",
        "obrigada pelo retorno", "obrigada pelo envio",
        "ficamos à disposição", "passará por análise",
        "vamos analisar", "em análise", "processaremos",
        "já chegou", "chegaram os extratos", "chegou o extrato",
        "recebemos os documentos", "documentos chegaram",
        "muito obrigada", "obrigada!", "obrigada,",
    ]
    # Encontra o timestamp do último agradecimento da Sâmea
    # Usa corpo LIMPO para não bater em "recebimento" do boilerplate padrão
    ts_ultimo_agradecimento = 0
    for h in historico[1:]:
        if h["eh_samea"]:
            corpo_limpo = _limpar_corpo_para_chatbot(h["corpo"], True).lower()
            if any(f in corpo_limpo for f in _FRASES_AGRADECIMENTO):
                ts_ultimo_agradecimento = max(ts_ultimo_agradecimento, h["timestamp"])

    # Se o cliente (externo) respondeu DEPOIS do agradecimento, reabre o caso
    cliente_respondeu_apos_agradecimento = any(
        not h["eh_interno"] and h["timestamp"] > ts_ultimo_agradecimento
        for h in historico[1:]
    )
    samea_ja_agradeceu = (
        ts_ultimo_agradecimento > 0 and not cliente_respondeu_apos_agradecimento
    )

    # samea_foi_verificar: Sâmea disse que ia conferir no app/sistema e não retornou
    # → cliente afirmou ter enviado, Sâmea foi checar offline, silêncio depois = envio correto
    _FRASES_VERIFICAR = [
        "vou verificar", "vou checar", "vou conferir", "vou consultar",
        "vou acessar", "vou acessar a conta", "vou acessar o sistema",
        "estou verificando", "já estou verificando", "verificarei",
        "vou olhar no", "vou checar no app", "vou verificar no app",
        "vou verificar no sistema", "vou conferir no sistema",
        "vou verificar aqui", "deixa eu verificar", "deixa eu checar",
    ]
    ts_ultima_verificacao = 0
    for h in historico[1:]:
        if h["eh_samea"]:
            corpo_limpo = _limpar_corpo_para_chatbot(h["corpo"], True).lower()
            if any(f in corpo_limpo for f in _FRASES_VERIFICAR):
                ts_ultima_verificacao = max(ts_ultima_verificacao, h["timestamp"])

    # Só fecha se o cliente (externo) não voltou a falar após a verificação
    cliente_respondeu_apos_verificacao = any(
        not h["eh_interno"] and h["timestamp"] > ts_ultima_verificacao
        for h in historico[1:]
    )
    samea_foi_verificar = (
        ts_ultima_verificacao > 0 and not cliente_respondeu_apos_verificacao
    )

    # prazo_vencido: Sâmea foi a última a falar, cliente respondeu sem anexo, +24h sem retorno
    # Não dispara se já agradecemos — contradição garantida
    prazo_vencido = False
    if (
        not samea_ja_agradeceu
        and ultima_de and EMAIL_SAMEA.lower() in ultima_de.lower()
        and cliente_respondeu
        and not tem_anexo_ultima
        and ultima_msg_cliente is not None
    ):
        ts_cliente    = ultima_msg_cliente.get("timestamp", 0)
        agora_ms      = datetime.now(TIMEZONE).timestamp() * 1000
        horas_passadas = (agora_ms - ts_cliente) / 3_600_000
        if horas_passadas >= 24:
            prazo_vencido = True

    return {
        "samea_iniciou":        samea_iniciou,
        "cliente_respondeu":    cliente_respondeu,
        "tem_anexo":            tem_anexo_cliente,
        "tem_anexo_ultima":     tem_anexo_ultima,
        "ultima_de":            ultima_de,
        "samea_corrigiu":       samea_corrigiu,
        "meses_correcao":       meses_correcao,
        "prazo_vencido":        prazo_vencido,
        "samea_ja_agradeceu":   samea_ja_agradeceu,
        "samea_foi_verificar":  samea_foi_verificar,
        "historico":            historico,
    }

# ╔══════════════════════════════════════════════════════════╗
# ║       CÁLCULO DOS MESES PENDENTES (POR CORPO!)           ║
# ╚══════════════════════════════════════════════════════════╝

def calcular_meses_pendentes_por_corpo(threads: list) -> list:
    # Usa apenas a PRIMEIRA mensagem com 📌 de cada thread.
    # Isso preserva a ordem original da solicitação e evita que reenvios
    # anteriores com meses incorretos contaminem a lista.
    pendentes, vistos = [], set()
    for thread in threads:
        analise   = thread.get("analise") or analisar_thread(thread)
        historico = analise["historico"]
        for msg in historico:
            if msg["eh_samea"] and msg["meses_marcados"]:
                for m in msg["meses_marcados"]:
                    if m not in vistos:
                        vistos.add(m)
                        pendentes.append(m)
                break  # para após a primeira msg com meses — não acumula reenvios
    return pendentes


def _assunto_pertence_cliente(assunto: str, nome: str) -> bool:
    assunto_u = assunto.upper()
    nome_u    = nome.upper().strip()
    # Formato padrão: assunto termina com "- NOME_EMPRESA"
    partes = assunto_u.split(" - ")
    if partes[-1].strip() == nome_u:
        return True
    # Fallback: nome aparece em qualquer parte (threads no formato antigo de testes)
    return nome_u in assunto_u


def montar_assunto(meses: list, nome_empresa: str) -> str:
    ano_curto = str(datetime.now(TIMEZONE).year)[-2:]
    meses_fmt = " E ".join(f"{m}/{ano_curto}" for m in meses)
    return f"SOLICITAÇÃO DE EXTRATOS BANCÁRIOS - {meses_fmt} - {nome_empresa.upper()}"


# ╔══════════════════════════════════════════════════════════╗
# ║         MÊS ALVO — SEMPRE O MÊS ANTERIOR                ║
# ║  Baseado no fuso horário de Manaus (America/Manaus)      ║
# ╚══════════════════════════════════════════════════════════╝

def _meses_alvo_padrao() -> list:
    """
    Retorna sempre o mês ANTERIOR ao mês atual, no fuso de Manaus.
    Exemplo: se hoje é qualquer dia de junho/2026 → retorna ["MAIO"]
             se hoje é qualquer dia de janeiro/2027 → retorna ["DEZEMBRO"]
    O cálculo (agora.month - 2) % 12 + 1 garante o wraparound correto
    para janeiro (mês 1 → dezembro do ano anterior = mês 12).
    """
    agora = datetime.now(TIMEZONE)
    numero_mes_anterior = (agora.month - 2) % 12 + 1
    return [MESES_PT[numero_mes_anterior]]

# ╔══════════════════════════════════════════════════════════╗
# ║              CORPOS HTML + ASSINATURA INLINE             ║
# ╚══════════════════════════════════════════════════════════╝

_DISCLAIMER = (
    "<br><br>"
    "As informações contidas nesta mensagem e quaisquer outras informações adicionais em arquivo(s) "
    "anexado(s), são confidenciais e seu sigilo protegido por lei, e somente o(s) destinatário(s) está "
    "(ao) autorizado(s) a fazer uso das mesmas. Caso não seja o destinatário pretendido e tenha recebido "
    "esta mensagem por engano, por favor, notifique o remetente e em seguida destrua este e-mail, "
    "observando que deverá abster-se: de divulgar, distribuir, examinar, armazenar, encaminhar, imprimir, "
    "copiar ou utilizar a informação contida em seu conteúdo, caso contrário, estará sujeito às sanções e "
    "penalidades da legislação em vigor. Os dados pessoais constantes nesta mensagem serão tratados de "
    "acordo com a finalidade para a qual foram coletados, utilizando-se de meios que garantam a proteção "
    "dos mesmos, em consonância com o Art. 6 da Lei Geral de Proteção de Dados Pessoais - LGPD - "
    "Lei 13.709/2018."
    "<br><br>"
    "The information contained in this message and any other additional information in attached file(s) is "
    "confidential and its secrecy protected by law, and only the recipient(s) is authorized to use it. "
    "If you are not the intended recipient and you have received this message in error, please notify the "
    "sender and then destroy this email, noting that you must refrain from: disclosing, distributing, "
    "examining, storing, forwarding, printing, copying or use the information contained in its content, "
    "otherwise, it will be subject to the sanctions and penalties of the legislation in force. The personal "
    "data contained in this message will be treated according to the purpose for which they were collected, "
    "using means that guarantee their protection, in accordance with Article 6 of the General Law on "
    "Personal Data Protection - LGPD - Law 13.709/2018."
)


def _html_base(conteudo: str) -> str:
    return f"""
<html><body>
<div style="font-family:Arial,sans-serif;font-size:14px;color:#222;line-height:1.6;">
{conteudo}
<br><br>
<b>Quaisquer necessidades conte comigo!</b>
<br><br>
<b>Peço a gentileza que assim que possível me confirme o recebimento.</b>
<br><br>
<img src="cid:assinatura_samea"
     alt="Sâmea Gomes — AKARTOS CONTABILIDADE"
     style="max-width:500px;display:block;margin-top:10px;">
{_DISCLAIMER}
</div>
</body></html>
""".strip()


def _meses_com_pin(meses: list) -> str:
    return "".join(f"📌&nbsp;<b>{m}</b><br>\n" for m in meses)


def corpo_solicitacao(nome: str, meses: list, bancos: list) -> str:
    bancos_str = ", ".join(bancos) or "todos os bancos cadastrados"
    conteudo = (
        f"Prezados,<br><br>"
        f"Espero que estejam bem!<br><br>"
        f"Estou entrando em contato para solicitar os extratos mensais consolidados da&nbsp;"
        f"<b>CONTA CORRENTE</b>&nbsp;e&nbsp;<b>APLICAÇÕES FINANCEIRAS/INVESTIMENTOS</b>&nbsp;"
        f"da empresa&nbsp;<b>{nome}</b>&nbsp;em formato&nbsp;<b>PDF</b>&nbsp;e&nbsp;<b>OFX</b>, "
        f"referente ao(s) seguinte(s) mês(es):<br><br><br>"
        f"{_meses_com_pin(meses)}"
        f"<br>"
        f"<b>BANCOS</b>: {bancos_str}<br><br>"
        f"O envio mensal dos extratos bancários é fundamental para a gestão financeira e tributária "
        f"da sua empresa. Manter a contabilidade atualizada garante que todas as suas operações "
        f"estejam em conformidade com a legislação.<br>"
        f"Fico no aguardo do seu retorno, caso você tenha dúvidas e precise de auxílio na emissão "
        f"dos extratos, nos informe, para que possamos ajudá-lo.&nbsp;😊🤝<br><br>"
        f"Atenciosamente,"
    )
    return _html_base(conteudo)


def corpo_reenvio(nome: str, meses: list, bancos: list) -> str:
    bancos_str = ", ".join(bancos) or "todos os bancos cadastrados"
    conteudo = (
        f"Prezados,<br><br>"
        f"Espero que estejam bem!<br><br>"
        f"Viemos reforçar nossa solicitação dos extratos mensais consolidados da&nbsp;"
        f"<b>CONTA CORRENTE</b>&nbsp;e&nbsp;<b>APLICAÇÕES FINANCEIRAS/INVESTIMENTOS</b>&nbsp;"
        f"da empresa&nbsp;<b>{nome}</b>, pois ainda não recebemos o retorno "
        f"dos seguintes meses:<br><br><br>"
        f"{_meses_com_pin(meses)}"
        f"<br>"
        f"<b>BANCOS</b>: {bancos_str}<br><br>"
        f"O envio mensal dos extratos bancários é fundamental para a gestão financeira e tributária "
        f"da sua empresa. Manter a contabilidade atualizada garante que todas as suas operações "
        f"estejam em conformidade com a legislação.<br>"
        f"Pedimos que nos encaminhe o quanto antes para darmos continuidade ao trabalho contábil. "
        f"Caso tenha dúvidas e precise de auxílio na emissão dos extratos, nos informe, para que "
        f"possamos ajudá-lo.&nbsp;😊🤝<br><br>"
        f"Atenciosamente,"
    )
    return _html_base(conteudo)


def corpo_agradecimento(nome: str) -> str:
    hora = datetime.now(TIMEZONE).hour
    if hora < 12:
        saudacao = "Bom dia! ☀️"
    elif hora < 18:
        saudacao = "Boa tarde! ☀️"
    else:
        saudacao = "Boa noite!"
    conteudo = (
        f"{saudacao}<br><br>"
        f"Acuso o recebimento dos extratos bancários de&nbsp;<b>{nome}</b>. "
        f"Muito obrigada! ✅<br><br>"
        f"Qualquer necessidade, estamos à disposição. 😊<br><br>"
        f"Atenciosamente,"
    )
    return _html_base(conteudo)


def corpo_chatbot(resposta_ia: str) -> str:
    conteudo = f"<p>{resposta_ia}</p><p>Atenciosamente,</p>"
    return _html_base(conteudo)

# ╔══════════════════════════════════════════════════════════╗
# ║                  CHATBOT — CLAUDE API                    ║
# ╚══════════════════════════════════════════════════════════╝

_claude_client = None

def _get_claude():
    global _claude_client
    if _claude_client is None:
        _claude_client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    return _claude_client


SYSTEM_CHATBOT = """
Você é Sâmea Gomes, assistente do departamento BPO Financeiro da AKARTOS CONTABILIDADE (contabilidade36@akartos.com.br).

Seu objetivo: garantir que os extratos bancários mensais solicitados sejam recebidos dos clientes.

Personalidade:
- Profissional e cordial, com tom formal porém acolhedor — como uma colaboradora experiente, não um robô.
- Linguagem cuidada, evita gírias e expressões muito informais, mas sem ser fria ou distante.
- Demonstra empatia genuína quando o cliente tem dificuldades ou dúvidas.
- Trata o cliente pelo nome quando disponível no histórico.

Ao receber o histórico da conversa, você deve:
1. Ler e entender TODA a conversa — o que foi pedido, o que o cliente respondeu, o que você (Sâmea) já disse antes.
2. Avaliar o estado atual: os extratos foram recebidos? A conversa está encerrada? O cliente respondeu algo que exige retorno?
3. Decidir e responder no formato EXATO abaixo — sem nenhum texto fora dele.

FORMATO DE SAÍDA OBRIGATÓRIO — escolha exatamente um:

Se não há nada a fazer:
NENHUMA_ACAO

Se há algo a responder:
RESPONDER
---
(escreva aqui somente o corpo do e-mail)

REGRAS CRÍTICAS:
- NUNCA escreva fora desse formato. Nenhum raciocínio, nenhuma explicação, nenhum prefácio.
- A primeira linha deve ser exatamente NENHUMA_ACAO ou RESPONDER — nada mais.
- Após RESPONDER, coloque exatamente "---" e depois o corpo do e-mail.
- Nunca contradiga mensagens suas anteriores na mesma conversa.
- Se o cliente disse que vai enviar e ainda não enviou → follow-up gentil.
- Se o cliente enviou mas você não confirmou → agradeça e informe que passará por análise.

Convenção interna: meses pendentes são marcados com 📌 (não explique ao cliente).
Notas internas: linhas "[NOTA INTERNA:]" são contexto — nunca as mencione.

Regras do corpo do e-mail:
- Português natural, formal mas acolhedor.
- Nunca revele que é IA ou sistema automatizado.
- Sem markdown: sem **, _, #.
- SEM saudação de abertura nem "Atenciosamente" — são adicionados automaticamente.
"""

_NENHUMA_ACAO = "NENHUMA_ACAO"


def gerar_resposta_chatbot(historico: list, contexto_extra: str = "") -> str | None:
    """Retorna o texto da resposta, ou None se o chatbot decidir não agir."""
    client = _get_claude()

    # Monta conversa real alternando user (cliente) / assistant (Sâmea)
    mensagens_api: list = []
    for h in historico[-12:]:
        role  = "assistant" if h["eh_samea"] else "user"
        corpo = _limpar_corpo_para_chatbot(h["corpo"], h["eh_samea"])[:2000]
        if mensagens_api and mensagens_api[-1]["role"] == role:
            mensagens_api[-1]["content"] += f"\n\n{corpo}"
        else:
            mensagens_api.append({"role": role, "content": corpo})

    # A API exige que comece com "user"
    while mensagens_api and mensagens_api[0]["role"] == "assistant":
        mensagens_api.pop(0)

    if not mensagens_api:
        return None

    # Injeta contexto extra como nota interna
    if contexto_extra:
        nota = f"\n\n[NOTA INTERNA: {contexto_extra}]"
        if mensagens_api[-1]["role"] == "user":
            mensagens_api[-1]["content"] += nota
        else:
            mensagens_api.append({"role": "user", "content": nota})

    # Garante que termina com "user"
    if mensagens_api[-1]["role"] == "assistant":
        mensagens_api.append({"role": "user", "content": "[NOTA INTERNA: analise a conversa e decida se deve responder.]"})

    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        system=SYSTEM_CHATBOT,
        messages=mensagens_api,
    )
    texto = resp.content[0].text.strip()

    # Formato esperado: primeira linha = NENHUMA_ACAO ou RESPONDER
    primeira_linha = texto.split("\n")[0].strip()

    if primeira_linha == _NENHUMA_ACAO or texto == _NENHUMA_ACAO:
        return None

    # Extrai apenas o corpo após "---"
    if "---" in texto:
        partes = texto.split("---", 1)
        texto = partes[1].strip()
    elif primeira_linha == "RESPONDER":
        # Sem separador mas com RESPONDER — remove só a primeira linha
        texto = "\n".join(texto.split("\n")[1:]).strip()

    if not texto:
        return None

    # Remove markdown residual
    texto = re.sub(r"\*\*(.+?)\*\*", r"\1", texto)
    texto = re.sub(r"\*(.+?)\*",     r"\1", texto)
    texto = re.sub(r"__(.+?)__",     r"\1", texto)
    texto = re.sub(r"_(.+?)_",       r"\1", texto)
    return texto

# ╔══════════════════════════════════════════════════════════╗
# ║               GMAIL — ENVIO COM ASSINATURA               ║
# ╚══════════════════════════════════════════════════════════╝

def _construir_mime(para, assunto, corpo_html, message_id_reply=None):
    msg            = MIMEMultipart("related")
    msg["From"]    = EMAIL_SAMEA
    msg["To"]      = para
    msg["Subject"] = assunto
    if message_id_reply:
        msg["In-Reply-To"] = message_id_reply
        msg["References"]  = message_id_reply
    msg.attach(MIMEText(corpo_html, "html", "utf-8"))

    if os.path.exists(ASSINATURA_IMG):
        with open(ASSINATURA_IMG, "rb") as f:
            img = MIMEImage(f.read())
        img.add_header("Content-ID", "<assinatura_samea>")
        img.add_header("Content-Disposition", "inline")
        msg.attach(img)
    else:
        print(f"    ⚠️  Assinatura não encontrada: {ASSINATURA_IMG}")
    return msg


def enviar_email(creds, para, assunto, corpo_html, thread_id=None, message_id_reply=None):
    service = build("gmail", "v1", credentials=creds)
    mime    = _construir_mime(para, assunto, corpo_html, message_id_reply)
    raw     = base64.urlsafe_b64encode(mime.as_bytes()).decode()
    body    = {"raw": raw}
    if thread_id:
        body["threadId"] = thread_id
    enviado = service.users().messages().send(userId="me", body=body).execute()
    print(f"    📧 Enviado → {para}  |  ID: {enviado['id']}")
    return enviado

# ╔══════════════════════════════════════════════════════════╗
# ║              SIMULAÇÃO NO GOOGLE DOCS                    ║
# ╚══════════════════════════════════════════════════════════╝

def simular_no_docs(creds, acoes: list):
    if not acoes:
        return
    service = build("docs", "v1", credentials=creds)
    doc     = service.documents().get(documentId=GOOGLE_DOC_SIMULACAO).execute()
    end     = doc.get("body", {}).get("content", [{}])[-1].get("endIndex", 1)

    # Só apaga se realmente houver conteúdo
    if end > 2:
        try:
            service.documents().batchUpdate(
                documentId=GOOGLE_DOC_SIMULACAO,
                body={"requests": [{"deleteContentRange": {
                    "range": {"startIndex": 1, "endIndex": end - 1}
                }}]},
            ).execute()
        except Exception as e:
            print(f"  ⚠️  Não consegui limpar o Doc (vou só anexar): {e}")

    # ── Classifica ações por TIPO ─────────────────────────────
    novos         = [a for a in acoes if a.get("tipo") == "novo"]
    reenvios      = [a for a in acoes if a.get("tipo") == "reenvio"]
    agradecimentos = [a for a in acoes if a.get("tipo") == "agradecimento"]
    chatbots      = [a for a in acoes if a.get("tipo") == "chatbot"]

    agora = datetime.now(TIMEZONE).strftime("%d/%m/%Y %H:%M")
    linhas = []

    # ── CABEÇALHO ─────────────────────────────────────────────
    linhas.append("╔" + "═"*70 + "╗\n")
    linhas.append("║   SIMULAÇÃO DE E-MAILS — AKARTOS CONTABILIDADE          ║\n")
    linhas.append(f"║   Gerado em: {agora}  |  Modo: {MODO.upper()}                      ║\n")
    linhas.append("╚" + "═"*70 + "╝\n\n")

    # ── RESUMO ────────────────────────────────────────────────
    linhas.append("📊 RESUMO\n")
    linhas.append("─" * 72 + "\n")
    linhas.append(f"   📨 Novos e-mails (1ª solicitação) ...... {len(novos)}\n")
    linhas.append(f"   🔁 Reenvios (cobrança consolidada) ...... {len(reenvios)}\n")
    linhas.append(f"   🙏 Agradecimentos (anexo recebido) ...... {len(agradecimentos)}\n")
    linhas.append(f"   🤖 Respostas contextuais (chatbot) ...... {len(chatbots)}\n")
    linhas.append("   " + "─"*30 + "\n")
    linhas.append(f"   TOTAL: {len(acoes)} e-mails\n\n")

    # ── Função auxiliar pra renderizar uma seção ─────────────
    def render_secao(titulo: str, icone: str, lista: list):
        if not lista:
            return
        linhas.append("\n")
        linhas.append("█" * 72 + "\n")
        linhas.append(f"  {icone}  {titulo}  ({len(lista)} e-mails)\n")
        linhas.append("█" * 72 + "\n\n")

        for i, a in enumerate(lista, 1):
            linhas.append(f"┌─ #{i:02d} " + "─"*64 + "┐\n")
            linhas.append(f"│  CLIENTE: {a['cliente']['nome']}\n")
            linhas.append(f"│  PARA:    {a['para']}\n")
            linhas.append(f"│  ASSUNTO: {a['assunto']}\n")
            if a.get("thread_id"):
                linhas.append(f"│  TIPO:    📌 RESPOSTA em thread existente\n")
            else:
                linhas.append(f"│  TIPO:    ✨ E-MAIL NOVO\n")
            linhas.append("├" + "─"*70 + "┤\n")
            _corpo_raw  = a.get("corpo_html", "")
            _corpo_raw  = re.sub(r"<br\s*/?>|</p>|</div>|</tr>", "\n", _corpo_raw, flags=re.IGNORECASE)
            corpo_limpo = html.unescape(re.sub(r"<[^>]+>", "", _corpo_raw)).strip()
            for linha_corpo in corpo_limpo.split("\n"):
                if linha_corpo.strip():
                    linhas.append(f"│  {linha_corpo.strip()}\n")
            linhas.append("└" + "─"*70 + "┘\n\n")

    # ── Renderiza cada categoria ──────────────────────────────
    render_secao("E-MAILS NOVOS — primeira solicitação", "📨", novos)
    render_secao("REENVIOS — cobrança de pendências", "🔁", reenvios)
    render_secao("AGRADECIMENTOS — anexos recebidos", "🙏", agradecimentos)
    render_secao("RESPOSTAS CONTEXTUAIS — chatbot",   "🤖", chatbots)

    # ── Escreve no Doc ────────────────────────────────────────
    service.documents().batchUpdate(
        documentId=GOOGLE_DOC_SIMULACAO,
        body={"requests": [{"insertText": {
            "location": {"index": 1},
            "text": "".join(linhas),
        }}]},
    ).execute()

    url = f"https://docs.google.com/document/d/{GOOGLE_DOC_SIMULACAO}/edit"
    print(f"\n  📄 Simulação no Google Docs → {url}")
    print(f"     Resumo: 📨 {len(novos)} novos  |  🔁 {len(reenvios)} reenvios  "
          f"|  🙏 {len(agradecimentos)} agrad.  |  🤖 {len(chatbots)} chatbot\n")

# ╔══════════════════════════════════════════════════════════╗
# ║              PROCESSAMENTO POR CLIENTE                   ║
# ╚══════════════════════════════════════════════════════════╝

def processar_cliente(creds, cliente: dict) -> list:
    nome   = cliente["nome"]
    email  = cliente["email_destino"]
    bancos = cliente["bancos"]
    acoes  = []

    print(f"  👤 {nome}  <{email}>")

    threads = buscar_threads_cliente(creds, cliente["email_real"])
    for t in threads:
        t["analise"] = analisar_thread(t)

    # Filtra por nome no assunto — evita que dois clientes com mesmo e-mail se contaminem
    threads_samea = [
        t for t in threads
        if t["analise"]["samea_iniciou"] and _assunto_pertence_cliente(t["assunto"], nome)
    ]
    # Ordena threads do mais antigo para o mais recente — garante ordem da 1ª solicitação
    threads_samea.sort(key=lambda t: int(t["mensagens"][0].get("internalDate", 0)) if t["mensagens"] else 0)

    meses_alvo = _meses_alvo_padrao()

    # threads_samea = todos os threads (usados para contexto/histórico)
    # threads_ativas = apenas após DATA_INICIO (usados para decidir ações)
    if DATA_INICIO:
        _ts_inicio = datetime.strptime(DATA_INICIO, "%d/%m/%Y").replace(tzinfo=TIMEZONE).timestamp() * 1000
        threads_ativas = [
            t for t in threads_samea
            if int(t["mensagens"][0].get("internalDate", 0)) >= _ts_inicio
        ]
    else:
        threads_ativas = threads_samea

    if not threads_ativas:
        # Sem thread ativa — sempre envia nova solicitação do mês anterior (ignora histórico antigo)
        assunto = montar_assunto(meses_alvo, nome)
        corpo   = corpo_solicitacao(nome, meses_alvo, bancos)
        print(f"    → 📨 Novo e-mail: {', '.join(meses_alvo)}")
        acoes.append({
            "tipo": "novo",
            "para": email, "assunto": assunto, "corpo_html": corpo,
            "thread_id": None, "message_id": None, "cliente": cliente,
        })
        return acoes

    # threads_ativas = após DATA_INICIO (para decidir ações)
    # threads_samea  = todos (para contexto/histórico do chatbot)

    # Threads fechadas e abertas — sobre as ativas apenas
    threads_fechadas = [
        t for t in threads_ativas
        if t["analise"].get("samea_ja_agradeceu") or t["analise"].get("samea_foi_verificar")
    ]
    threads_abertas = [t for t in threads_ativas if t not in threads_fechadas]

    # ── Ignora threads com mais de um mês no assunto onde a Sâmea já atuou ──────
    # Essas threads são de ciclos anteriores tratados manualmente; não devem
    # receber ação automática para não poluir conversas já encerradas/complexas.
    def _assunto_tem_multiplos_meses(assunto: str) -> bool:
        return len(extrair_meses_livres(assunto)) > 1

    def _samea_atuou(t: dict) -> bool:
        """Sâmea enviou pelo menos uma mensagem nesta thread (além da abertura)."""
        return any(h["eh_samea"] for h in t["analise"]["historico"][1:])

    threads_abertas = [
        t for t in threads_abertas
        if not (_assunto_tem_multiplos_meses(t["assunto"]) and _samea_atuou(t))
    ]
    for t in [x for x in threads_ativas if x not in threads_fechadas and x not in threads_abertas]:
        print(f"    → ⏭️  Thread ignorada (multi-mês + Sâmea atuou): {t['assunto'][:60]}")

    # Meses já solicitados APENAS nas threads abertas (em andamento)
    # Threads fechadas de meses anteriores não bloqueiam novo pedido do mês atual
    meses_em_aberto: set = set()
    for t in threads_abertas:
        for h in t["analise"]["historico"]:
            if h["eh_samea"] and h.get("meses_marcados"):
                meses_em_aberto.update(h["meses_marcados"])

    # Meses alvo que não estão em nenhuma thread aberta
    meses_novos = [m for m in meses_alvo if m not in meses_em_aberto]

    # Última mensagem da Sâmea em TODAS as threads deste cliente (não só a thread atual)
    # Detecta retrações/esclarecimentos enviados em threads cruzadas
    _ultima_samea_global: dict | None = None
    for _t in threads_samea:
        for _h in reversed(_t["analise"]["historico"]):
            if _h["eh_samea"]:
                if _ultima_samea_global is None or _h["timestamp"] > _ultima_samea_global["timestamp"]:
                    _ultima_samea_global = _h
                break
    _samea_manual_global = (
        _ultima_samea_global is not None
        and not _ultima_samea_global.get("meses_marcados")
    )

    # Pendentes apenas das threads que irão gerar reenvio automático.
    # Threads onde cliente respondeu (→ chatbot) ou Sâmea enviou mensagem manual sem 📌
    # em QUALQUER thread (→ chatbot) não entram nos pendentes.
    def _vai_para_reenvio(t: dict) -> bool:
        if t["analise"]["cliente_respondeu"]:
            return False
        # Checa a thread específica
        ultima_s = next((h for h in reversed(t["analise"]["historico"]) if h["eh_samea"]), None)
        if ultima_s and not ultima_s.get("meses_marcados"):
            return False
        # Checa globalmente: se a msg mais recente da Sâmea em QUALQUER thread é manual
        if _samea_manual_global:
            return False
        return True

    threads_para_reenvio = [t for t in threads_abertas if _vai_para_reenvio(t)]
    pendentes_globais = calcular_meses_pendentes_por_corpo(threads_para_reenvio)

    if meses_novos:
        if threads_para_reenvio:
            # Há threads que vão para reenvio automático → injeta o novo mês nos pendentes
            # para o reenvio consolidar tudo em uma única mensagem na thread existente
            pendentes_globais = list(dict.fromkeys(pendentes_globais + meses_novos))
            print(f"    → 📨 Novo mês ({', '.join(meses_novos)}) consolidado no reenvio da thread existente")
        else:
            # Sem threads para reenvio → cria nova thread só com o mês novo
            assunto = montar_assunto(meses_novos, nome)
            corpo   = corpo_solicitacao(nome, meses_novos, bancos)
            print(f"    → 📨 Novo e-mail ({', '.join(meses_novos)}): nova solicitação mensal")
            acoes.append({
                "tipo": "novo",
                "para": email, "assunto": assunto, "corpo_html": corpo,
                "thread_id": None, "message_id": None, "cliente": cliente,
            })
        # Não retorna — threads abertas com cliente respondeu ainda precisam de chatbot
    print(f"    🔎 Meses pendentes (📌 no corpo): "
          f"{', '.join(pendentes_globais) if pendentes_globais else 'nenhum'}")

    # Flags para consolidar e evitar duplicatas
    ja_agradeceu = False
    ja_reenviou  = False
    ja_chatbot   = False

    # Histórico completo de TODAS as threads (inclusive antigas) para o chatbot ter contexto
    historico_completo = []
    for t in threads_samea:
        historico_completo.extend(t["analise"]["historico"])
    historico_completo.sort(key=lambda h: h.get("timestamp", 0))

    for thread in threads_ativas:
        analise   = thread["analise"]
        historico = historico_completo  # chatbot vê tudo, inclusive histórico antes de DATA_INICIO
        tid       = thread["thread_id"]

        # Se já agradecemos o recebimento em execução anterior, caso encerrado
        if analise.get("samea_ja_agradeceu"):
            print("    → ✅ Recebimento já confirmado em mensagem anterior — nenhuma ação")
            continue

        # Sâmea foi verificar no sistema/app e não retornou = envio foi correto, caso encerrado
        if analise.get("samea_foi_verificar"):
            print("    → ✅ Sâmea foi verificar no sistema — envio considerado correto, nenhuma ação")
            continue
        last_msg  = thread["mensagens"][-1]
        message_id = next(
            (h["value"] for h in last_msg.get("payload", {}).get("headers", [])
             if h["name"].lower() == "message-id"), None
        )

        if analise["cliente_respondeu"]:
            # Delega ao chatbot: ele lê a conversa inteira e decide o que fazer
            if ja_chatbot:
                print("    → ⏭️  Já há resposta chatbot para este cliente, pulando")
                continue
            ja_chatbot = True
            print("    → 🤖 Chatbot analisando conversa...")

            # Monta contexto com informações que o chatbot não consegue extrair da conversa
            agora_ms = datetime.now(TIMEZONE).timestamp() * 1000
            ultima_msg_cliente = next(
                (h for h in reversed(historico) if not h["eh_samea"]), None
            )
            horas_desde_cliente = (
                (agora_ms - ultima_msg_cliente["timestamp"]) / 3_600_000
                if ultima_msg_cliente else 0
            )
            dias_desde_cliente = int(horas_desde_cliente / 24)

            ctx_estado = (
                f"Data/hora atual: {datetime.now(TIMEZONE).strftime('%d/%m/%Y %H:%M')}. "
                f"Última mensagem do cliente: há {dias_desde_cliente} dia(s). "
                f"Meses solicitados nesta conversa: {', '.join(pendentes_globais) if pendentes_globais else 'não identificados'} "
                f"(leia a conversa para saber o estado real — o cliente pode ter respondido sobre eles). "
                f"Bancos solicitados: {', '.join(bancos)}. "
                f"Anexo recebido na última mensagem do cliente: {'sim' if analise['tem_anexo_ultima'] else 'não'}."
            )

            try:
                resposta = gerar_resposta_chatbot(
                    historico,
                    contexto_extra=ctx_estado,
                )
            except Exception as e:
                print(f"    ⚠️  Chatbot indisponível ({e}). Nenhuma ação gerada.")
                resposta = None

            if resposta is None:
                print("    → ✅ Chatbot decidiu: nenhuma ação necessária")
            else:
                # Determina o tipo pelo conteúdo da resposta
                corpo_lower = resposta.lower()
                if any(f in corpo_lower for f in ["recebi", "obrigada pelo envio", "obrigado pelo envio", "vamos processar", "análise"]):
                    tipo_acao = "agradecimento"
                else:
                    tipo_acao = "chatbot"
                print(f"    → {'🙏' if tipo_acao == 'agradecimento' else '🤖'} Chatbot responde ({tipo_acao})")
                acoes.append({
                    "tipo": tipo_acao,
                    "para": email, "assunto": f"Re: {thread['assunto']}",
                    "corpo_html": corpo_chatbot(resposta),
                    "thread_id": tid, "message_id": message_id,
                    "cliente": cliente,
                })

        else:
            # Antes do reenvio: verifica se Sâmea enviou mensagem manual nesta thread
            # (última mensagem dela sem 📌 = retratação, esclarecimento, etc.)
            # Nesse caso o chatbot decide, não o reenvio automático.
            ultima_samea = next(
                (h for h in reversed(analise["historico"]) if h["eh_samea"]), None
            )
            samea_enviou_manual = (
                ultima_samea is not None
                and not ultima_samea.get("meses_marcados")
            ) or _samea_manual_global  # também bloqueia se msg global da Sâmea foi manual

            if samea_enviou_manual:
                if ja_chatbot:
                    print("    → ⏭️  Já há resposta chatbot para este cliente, pulando")
                    continue
                ja_chatbot = True
                print("    → 🤖 Sâmea enviou mensagem manual — chatbot avalia continuidade...")
                ctx_estado = (
                    f"Data/hora atual: {datetime.now(TIMEZONE).strftime('%d/%m/%Y %H:%M')}. "
                    f"Sâmea enviou uma mensagem personalizada nesta thread (sem 📌). "
                    f"Meses solicitados: {', '.join(pendentes_globais) if pendentes_globais else 'não identificados'} "
                    f"(leia a conversa para entender o estado real). "
                    f"Bancos: {', '.join(bancos)}."
                )
                try:
                    resposta = gerar_resposta_chatbot(historico, contexto_extra=ctx_estado)
                except Exception as e:
                    print(f"    ⚠️  Chatbot indisponível ({e}). Nenhuma ação gerada.")
                    resposta = None
                if resposta is None:
                    print("    → ✅ Chatbot decidiu: nenhuma ação necessária")
                else:
                    corpo_lower = resposta.lower()
                    tipo_acao = "agradecimento" if any(f in corpo_lower for f in [
                        "recebi", "obrigada pelo envio", "vamos processar", "análise"
                    ]) else "chatbot"
                    print(f"    → {'🙏' if tipo_acao == 'agradecimento' else '🤖'} Chatbot responde ({tipo_acao})")
                    acoes.append({
                        "tipo": tipo_acao,
                        "para": email, "assunto": f"Re: {thread['assunto']}",
                        "corpo_html": corpo_chatbot(resposta),
                        "thread_id": tid, "message_id": message_id,
                        "cliente": cliente,
                    })

            else:
                # Reenvio — consolida em UM SÓ por cliente na thread existente
                if ja_reenviou:
                    print("    → ⏭️  Já há reenvio para este cliente, pulando")
                    continue
                # Usa os pendentes globais (de TODAS as threads), não só desta
                meses_final = pendentes_globais or _meses_alvo_padrao()
                print(f"    → 🔁 Reenvio CONSOLIDADO: {', '.join(meses_final)}")
                ja_reenviou = True
                acoes.append({
                    "tipo": "reenvio",
                    "para": email, "assunto": f"Re: {thread['assunto']}",
                    "corpo_html": corpo_reenvio(nome, meses_final, bancos),
                    "thread_id": tid, "message_id": message_id,
                    "cliente": cliente,
                })

    return acoes

# ╔══════════════════════════════════════════════════════════╗
# ║              APROVAÇÃO INTERATIVA NO TERMINAL            ║
# ╚══════════════════════════════════════════════════════════╝

_ICONE_TIPO = {
    "novo":          "📨",
    "reenvio":       "🔁",
    "agradecimento": "🙏",
    "chatbot":       "🤖",
}

def aprovar_emails(acoes: list) -> list:
    if not acoes:
        print("\n  ✅ Nenhum e-mail para enviar.\n")
        return []

    # Lista numerada
    print(f"\n{'─'*65}")
    print(f"  {len(acoes)} e-mail(s) prontos para envio:\n")
    for i, a in enumerate(acoes, 1):
        icone = _ICONE_TIPO.get(a.get("tipo", ""), "📧")
        nome  = a["cliente"]["nome"][:28].ljust(28)
        print(f"  [{i:>2}] {icone}  {nome}  {a['para']}")
    print(f"\n{'─'*65}")
    print("  [T] Enviar todos")
    print("  [S] Selecionar quais enviar  (ex: 1,3  ou  2-4  ou  1 3 5)")
    print("  [C] Cancelar")
    print(f"{'─'*65}")

    while True:
        resp = input("\n  Opção: ").strip().lower()

        if resp in ("t", "todos", "s", "sim", "y", "yes", ""):
            return acoes

        if resp == "c" or resp in ("n", "nao", "não", "no"):
            print("  ❌ Cancelado.")
            return []

        # Tenta interpretar como seleção de números
        numeros: set[int] = set()
        tokens = re.split(r"[,\s]+", resp)
        valido = True
        for tok in tokens:
            if not tok:
                continue
            intervalo = re.fullmatch(r"(\d+)-(\d+)", tok)
            if intervalo:
                a_, b_ = int(intervalo.group(1)), int(intervalo.group(2))
                numeros.update(range(a_, b_ + 1))
            elif tok.isdigit():
                numeros.add(int(tok))
            else:
                valido = False
                break

        if not valido or not numeros:
            print("  ⚠️  Opção inválida. Digite T, S, C ou números (ex: 1,3 ou 2-4).")
            continue

        fora = [n for n in sorted(numeros) if n < 1 or n > len(acoes)]
        if fora:
            print(f"  ⚠️  Número(s) fora do intervalo: {fora}. Use de 1 a {len(acoes)}.")
            continue

        selecionados = [acoes[n - 1] for n in sorted(numeros)]
        print(f"\n  {len(selecionados)} e-mail(s) selecionado(s):")
        for a in selecionados:
            icone = _ICONE_TIPO.get(a.get("tipo", ""), "📧")
            print(f"    {icone}  {a['cliente']['nome']}  →  {a['para']}")
        confirma = input("\n  Confirmar envio? [S/n]: ").strip().lower()
        if confirma in ("", "s", "sim", "y", "yes"):
            return selecionados
        print("  Voltando à seleção...")

# ╔══════════════════════════════════════════════════════════╗
# ║                          MAIN                            ║
# ╚══════════════════════════════════════════════════════════╝

def main():
    agora = datetime.now(TIMEZONE).strftime("%d/%m/%Y %H:%M")
    print("\n" + "═"*60)
    print("  AKARTOS CONTABILIDADE — Automação de Extratos Bancários")
    print(f"  Modo: {'🧪 TESTE' if MODO == 'teste' else '🚀 REAL'}  |  {agora}")
    print(f"  Aba lida: '{ABA_CLIENTES}'  |  Filtro: coluna J preenchida")
    print("  Clientes com 'IGNORAR' na coluna M são automaticamente ignorados")
    print("═"*60 + "\n")

    print("🔐 Autenticando no Google...")
    creds = get_credentials()
    print("✅ Autenticado!\n")

    print(f"📋 Lendo aba '{ABA_CLIENTES}'...")
    clientes, _sem_email_log = listar_clientes(creds)
    print(f"   {len(clientes)} cliente(s) com e-mail de teste preenchido.\n")
    if not clientes:
        print("⚠️  Nenhum cliente. Encerrando.")
        sys.exit(0)

    # Sincroniza aba CLIENTES com ID, CLIENTE, E-MAIL
    try:
        atualizar_aba_clientes(creds, clientes)
    except Exception as _e:
        print(f"  ⚠️  Não foi possível atualizar aba CLIENTES: {_e}")

    # Mostra preview
    print("─"*60)
    for c in clientes[:5]:
        print(f"   • {c['nome']}  →  {c['email_destino']}")
    if len(clientes) > 5:
        print(f"   ... e mais {len(clientes)-5}")
    print("─"*60 + "\n")

    todas_acoes = []
    # ── LOG ───────────────────────────────────────────────────────
    _log_inicio   = datetime.now(TIMEZONE).strftime("%d/%m/%Y %H:%M:%S")
    _entradas_log: list = []
    # ──────────────────────────────────────────────────────────────
    print("🔍 Analisando e-mails (lendo CORPO + 📌)...\n")
    _preview_analise: list = []
    for cliente in clientes:
        try:
            acoes = processar_cliente(creds, cliente)
            todas_acoes.extend(acoes)
            _entradas_log.append({"cliente": cliente, "acoes": acoes, "erro": None})   # ── LOG
            # Meses mencionados no assunto das ações
            meses_acao = []
            for a in acoes:
                meses_acao += extrair_meses_livres(a.get("assunto", ""))
            _preview_analise.append({
                "cliente":      cliente,
                "threads_samea": [],
                "pendentes":    list(dict.fromkeys(meses_acao)),
                "acao":         " + ".join(a["tipo"].upper() for a in acoes) if acoes else "NENHUMA",
                "tipo_acao":    acoes[0]["tipo"] if acoes else "—",
            })
        except Exception as e:
            print(f"    ❌ Erro ao processar {cliente['nome']}: {e}")
            _entradas_log.append({"cliente": cliente, "acoes": [], "erro": str(e)})    # ── LOG
            _preview_analise.append({
                "cliente": cliente, "threads_samea": [],
                "pendentes": [], "acao": f"ERRO: {e}", "tipo_acao": "erro",
            })

    print(f"\n{'─'*60}")
    print(f"  Total de ações geradas: {len(todas_acoes)}")
    print(f"{'─'*60}")

    # Atualiza tabela de situação no Sheets antes de pedir aprovação
    try:
        atualizar_preview_sheets(creds, _preview_analise)
    except Exception as _e:
        print(f"  ⚠️  Não foi possível atualizar situação no Sheets: {_e}")

    if todas_acoes:
        print("\n📄 Gravando simulação no Google Docs...")
        simular_no_docs(creds, todas_acoes)

    aprovados = aprovar_emails(todas_acoes)
    if not aprovados:
        print("\n❌ Nenhum e-mail aprovado. Encerrando.\n")
        # ── LOG ──
        try:
            registrar_log_sheets(creds, _log_inicio, _entradas_log, [], _sem_email_log)
            print("  📊 Log gravado no Google Sheets (aba GERAL).\n")
        except Exception as _e:
            print(f"  ⚠️  Falha ao gravar log: {_e}")
        # ─────────
        sys.exit(0)

    print(f"\n📤 Enviando {len(aprovados)} e-mail(s)...\n")
    for acao in aprovados:
        try:
            enviar_email(
                creds,
                para=acao["para"],
                assunto=acao["assunto"],
                corpo_html=acao["corpo_html"],
                thread_id=acao.get("thread_id"),
                message_id_reply=acao.get("message_id"),
            )
            atualizar_situacao(creds, acao["cliente"]["linha_planilha"], "ENVIADO")
        except Exception as e:
            print(f"    ❌ Erro: {e}")
            atualizar_situacao(creds, acao["cliente"]["linha_planilha"], f"ERRO: {e}")

    print("\n✅ Processo concluído!\n")
    # ── LOG ───────────────────────────────────────────────────────
    try:
        registrar_log_sheets(creds, _log_inicio, _entradas_log, aprovados, _sem_email_log)
        print("  📊 Log gravado no Google Sheets (aba GERAL).\n")
    except Exception as _e:
        print(f"  ⚠️  Falha ao gravar log: {_e}")
    # ──────────────────────────────────────────────────────────────


# ╔══════════════════════════════════════════════════════════════════════╗
# ║  PREVIEW DE SITUAÇÃO — aba SITUAÇÃO ATUAL do Google Sheets           ║
# ╚══════════════════════════════════════════════════════════════════════╝

def atualizar_preview_sheets(creds, entradas_analise: list):
    service = build("sheets", "v4", credentials=creds)
    aba = "SITUAÇÃO ATUAL"

    cabecalho = [
        "EMPRESA", "E-MAIL", "MESES PENDENTES",
        "ÚLTIMA MSG CLIENTE", "DIAS SEM RESPOSTA",
        "AÇÃO PROPOSTA", "TIPO",
    ]

    linhas = [cabecalho]
    agora_ms = datetime.now(TIMEZONE).timestamp() * 1000

    for e in entradas_analise:
        cliente  = e["cliente"]
        nome     = cliente["nome"]
        email_d  = cliente["email_destino"]
        pendentes = ", ".join(e.get("pendentes") or []) or "—"
        acao     = e.get("acao", "NENHUMA")
        tipo     = e.get("tipo_acao", "—")

        ultima_data = "—"
        dias_str    = "—"
        for t in (e.get("threads_samea") or []):
            for h in reversed(t["analise"]["historico"]):
                if not h["eh_samea"] and h.get("timestamp"):
                    ts = h["timestamp"]
                    ultima_data = datetime.fromtimestamp(ts / 1000, TIMEZONE).strftime("%d/%m/%Y %H:%M")
                    dias_str    = str(int((agora_ms - ts) / 86_400_000))
                    break
            if ultima_data != "—":
                break

        linhas.append([nome, email_d, pendentes, ultima_data, dias_str, acao, tipo])

    meta = service.spreadsheets().get(spreadsheetId=PLANILHA_SITUACAO_ID).execute()
    abas_existentes = [s["properties"]["title"] for s in meta.get("sheets", [])]
    if aba not in abas_existentes:
        service.spreadsheets().batchUpdate(
            spreadsheetId=PLANILHA_SITUACAO_ID,
            body={"requests": [{"addSheet": {"properties": {"title": aba}}}]},
        ).execute()

    service.spreadsheets().values().clear(
        spreadsheetId=PLANILHA_SITUACAO_ID, range=f"'{aba}'!A:G"
    ).execute()
    service.spreadsheets().values().update(
        spreadsheetId=PLANILHA_SITUACAO_ID,
        range=f"'{aba}'!A1",
        valueInputOption="USER_ENTERED",
        body={"values": linhas},
    ).execute()
    print(f"  📊 Situação atualizada no Google Sheets (aba '{aba}') — {len(linhas)-1} cliente(s).")


# ╔══════════════════════════════════════════════════════════════════════╗
# ║  LOG DE EXECUÇÃO — aba GERAL do Google Sheets                        ║
# ╚══════════════════════════════════════════════════════════════════════╝

def registrar_log_sheets(creds, inicio: str, entradas: list, aprovados: list, sem_email: list = None):
    service = build("sheets", "v4", credentials=creds)
    aba = "GERAL"
    sem_email = sem_email or []

    _CABECALHO = ["ID", "EMPRESA", "E-MAIL", "BPO", "STATUS", "HISTÓRICO", "CONCLUÍDA?", "DESCRIÇÃO"]
    res_cab = service.spreadsheets().values().get(
        spreadsheetId=PLANILHA_SITUACAO_ID,
        range=f"'{aba}'!A3:H3",
    ).execute()
    linha3 = (res_cab.get("values") or [[]])[0]
    if not linha3 or linha3[0] != "ID":
        service.spreadsheets().values().update(
            spreadsheetId=PLANILHA_SITUACAO_ID,
            range=f"'{aba}'!A3:H3",
            valueInputOption="USER_ENTERED",
            body={"values": [_CABECALHO]},
        ).execute()
        print("  🔧 Cabeçalho da aba GERAL restaurado automaticamente.")

    res = service.spreadsheets().values().get(
        spreadsheetId=PLANILHA_SITUACAO_ID,
        range=f"'{aba}'!A:A",
    ).execute()
    n_linhas = len(res.get("values", []))
    proxima  = max(n_linhas + 1, 4)

    aprovados_keys = {(a["para"], a["assunto"], a["tipo"]) for a in aprovados}
    _id = proxima - 3

    linhas = []

    linhas.append([f"══ EXECUÇÃO: {inicio} ══", "", "", "", "", "", "", ""])

    for entrada in entradas:
        nome  = entrada["cliente"]["nome"]
        email = entrada["cliente"]["email_destino"]
        acoes = entrada["acoes"]
        erro  = entrada["erro"]

        if erro:
            _id += 1
            linhas.append([
                _id, nome, email, EMAIL_SAMEA,
                "ERRO", "", "Não",
                f"[{inicio}] Erro durante o processamento: {erro}",
            ])

        elif not acoes:
            _id += 1
            linhas.append([
                _id, nome, email, EMAIL_SAMEA,
                "NENHUMA AÇÃO", "", "—",
                (
                    f"[{inicio}] Nenhuma ação necessária para este cliente. "
                    "Possíveis razões: extratos já recebidos e agradecidos, "
                    "cliente já respondeu e aguarda retorno, "
                    "ou e-mail de correção já enviado pela Sâmea e cliente ainda não respondeu."
                ),
            ])

        else:
            for acao in acoes:
                _id += 1
                tipo  = acao["tipo"].upper()
                chave = (acao["para"], acao["assunto"], acao["tipo"])
                concluida = "Sim" if chave in aprovados_keys else "Não (não aprovado)"

                corpo = acao.get("corpo_html", "")
                meses_raw = re.findall(
                    r"\b(janeiro|fevereiro|março|abril|maio|junho|julho|"
                    r"agosto|setembro|outubro|novembro|dezembro)\b",
                    corpo, flags=re.IGNORECASE,
                )
                meses_str = ", ".join(dict.fromkeys(m.capitalize() for m in meses_raw))

                desc_tipo = {
                    "novo":          "Primeira solicitação de extratos bancários enviada.",
                    "reenvio":       f"Reenvio de cobrança — meses pendentes: {meses_str or 'não identificados'}.",
                    "agradecimento": "Agradecimento pelo recebimento dos extratos.",
                    "chatbot":       "Resposta via chatbot a mensagem enviada pelo cliente.",
                }.get(acao["tipo"], tipo)

                linhas.append([
                    _id, nome, email, EMAIL_SAMEA,
                    tipo, meses_str, concluida,
                    f"[{inicio}] {desc_tipo} | Assunto: {acao['assunto']}",
                ])

    if sem_email:
        _id += 1
        nomes_sem = ", ".join(c["nome"] for c in sem_email)
        linhas.append([
            _id, f"({len(sem_email)} clientes sem e-mail)", "—", EMAIL_SAMEA,
            "SEM E-MAIL", "", "—",
            f"[{inicio}] {len(sem_email)} cliente(s) sem e-mail cadastrado — não processados. Empresas: {nomes_sem}",
        ])

    # 2 linhas em branco após cada execução para separar visualmente
    linhas.append(["", "", "", "", "", "", "", ""])
    linhas.append(["", "", "", "", "", "", "", ""])

    service.spreadsheets().values().append(
        spreadsheetId=PLANILHA_SITUACAO_ID,
        range=f"'{aba}'!A{proxima}",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": linhas},
    ).execute()


def atualizar_aba_clientes(creds, clientes: list):
    """Sincroniza a aba CLIENTES com ID, CLIENTE, E-MAIL — sobrescreve a cada execução."""
    service = build("sheets", "v4", credentials=creds)
    aba = "CLIENTES"

    cabecalho = [["ID", "CLIENTE", "E-MAIL"]]
    rows = cabecalho + [
        [c["linha_planilha"] - 1, c["nome"], c["email_destino"]]
        for c in clientes
    ]

    # Limpa a aba antes de reescrever
    service.spreadsheets().values().clear(
        spreadsheetId=PLANILHA_SITUACAO_ID,
        range=f"'{aba}'!A:C",
    ).execute()

    service.spreadsheets().values().update(
        spreadsheetId=PLANILHA_SITUACAO_ID,
        range=f"'{aba}'!A1",
        valueInputOption="USER_ENTERED",
        body={"values": rows},
    ).execute()


if __name__ == "__main__":
    main()