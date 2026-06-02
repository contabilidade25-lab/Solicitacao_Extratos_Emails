# CONTEXTO DO PROJETO — AKARTOS CONTABILIDADE
# Automação de Extratos Bancários (automacao_samea.py)
# Última atualização: 01/06/2026

---

## QUEM É O USUÁRIO
- Jonas — CEO da AKARTOS CONTABILIDADE (kasseus@akartos.com.br)
- Desenvolve automações para o próprio escritório contábil
- Máquina: Windows 11, Python 3.14, VS Code com Claude Code
- Fuso: America/Manaus (UTC-4)
- Preferências: respostas diretas e curtas, sem negrito (**) nas respostas, não alterar código existente sem necessidade

---

## O QUE O SISTEMA FAZ
Script `c:\solicitacaoextrato\automacao_samea.py` (~1350 linhas).
Automação que substitui o trabalho manual de solicitar extratos bancários mensalmente para ~25 clientes via Gmail.

Executa como `.py` direto ou como `.exe` (PyInstaller em `dist\AKARTOS_Extratos\`).

---

## PESSOAS E E-MAILS
- `EMAIL_SAMEA = "contabilidade36@akartos.com.br"` — Sâmea Gomes (assina os e-mails, é a "persona" do chatbot)
- `EMAIL_GERENTE = "contabilidade16@akartos.com.br"` — Gerente
- `EMAIL_CEO = "kasseus@akartos.com.br"` — Jonas

---

## INTEGRAÇÕES
- Google Sheets: lê clientes de `PLANILHA_CLIENTES_ID` (aba "SOLICITAÇÕES - RASCUNHO"), grava log em `PLANILHA_SITUACAO_ID` (aba "GERAL")
- Gmail: busca threads por cliente, envia e-mails HTML com assinatura inline (`ramals/ramal_samea.png`)
- Google Docs: preview antes do envio (`GOOGLE_DOC_SIMULACAO`)
- Claude API: chatbot contextual (`claude-sonnet-4-6`, `CLAUDE_API_KEY` no topo do arquivo)

---

## COLUNAS DA PLANILHA (0-based)
- `COL_NOME = 0` — A: Nome da empresa
- `COL_EMAIL = 1` — B: E-mail real do cliente
- `COL_BANCOS = 5` — F: Bancos a serem solicitados
- `COL_TESTE = 9` — J: E-mail de TESTE
- `COL_IGNORAR = 12` — M: Se contiver "IGNORAR", pula o cliente
- `MODO = "real"` → usa coluna B; `MODO = "teste"` → usa coluna J

---

## FLUXO PRINCIPAL (processar_cliente)

### Sem threads anteriores:
→ 📨 Novo e-mail com meses padrão (`_meses_alvo_padrao()` = mês anterior)

### Com threads (cliente_respondeu = True):
Verificações de encerramento ANTES de qualquer ação:
1. `samea_ja_agradeceu` = True E cliente não voltou → ✅ caso encerrado
2. `samea_foi_verificar` = True E cliente não voltou → ✅ caso encerrado

Se não encerrado:
→ 🤖 Chatbot lê a conversa inteira e DECIDE autonomamente o que fazer
   - Retorna `NENHUMA_ACAO` → nenhum e-mail
   - Retorna texto → envia como agradecimento ou chatbot

### Sem resposta do cliente (cliente_respondeu = False):
→ 🔁 Reenvio consolidado com meses pendentes (📌 no corpo)

---

## CHATBOT — FUNCIONAMENTO ATUAL
**MUDANÇA IMPORTANTE:** O chatbot não recebe mais contextos injetados por código (era: "o cliente disse X, faça Y"). Agora ele lê a conversa inteira e decide sozinho.

`gerar_resposta_chatbot(historico, contexto_extra="")`:
- Monta conversa real alternando `user` (cliente) / `assistant` (Sâmea)
- Usa `_limpar_corpo_para_chatbot()` para remover boilerplate antes de passar ao Claude
- Retorna `None` se chatbot decide NENHUMA_ACAO
- Retorna texto da resposta caso contrário
- Remove markdown residual (`**`, `*`, `__`, `_`) automaticamente

`SYSTEM_CHATBOT` instrui o modelo a:
- Ler toda a conversa e entender o estado atual
- Decidir se deve responder ou retornar NENHUMA_ACAO
- Nunca contradizer mensagens anteriores
- Não usar markdown
- Não incluir saudação de abertura nem "Atenciosamente"

---

## DETECÇÃO DE ANEXOS (_tem_anexo)
Ignora:
- Imagens com `Content-ID` (cid:...) ou `Content-Disposition: inline` → assinatura visual/logo
- MIME types de assinatura digital S/MIME: `application/pkcs7-signature`, `application/x-pkcs7-signature`, `application/pkcs7-mime`, `application/x-pkcs7-mime`
- Arquivos `smime.p7s`, `smime.p7m`, `smime.p7b`

Conta como anexo real: PDFs, OFX, qualquer outro arquivo não enquadrado acima.

`tem_anexo_ultima`: verifica apenas a última mensagem DO CLIENTE (não toda a thread).

---

## FLAGS DE analisar_thread
- `samea_iniciou`: primeira mensagem foi da Sâmea
- `cliente_respondeu`: cliente respondeu em algum momento
- `tem_anexo`: alguma mensagem do cliente teve anexo real
- `tem_anexo_ultima`: última mensagem do cliente tem anexo real
- `samea_corrigiu`: última mensagem da Sâmea contém frases de correção
- `meses_correcao`: meses identificados na mensagem de correção
- `prazo_vencido`: Sâmea foi a última + cliente respondeu sem anexo + 24h passadas
- `samea_ja_agradeceu`: Sâmea já confirmou recebimento E cliente não voltou depois
- `samea_foi_verificar`: Sâmea disse que ia checar no sistema/app E cliente não voltou depois

---

## LIMPEZA DE CORPO PARA CHATBOT (_limpar_corpo_para_chatbot)
Para mensagens da Sâmea: corta tudo a partir de frases de boilerplate:
- "Quaisquer necessidades conte comigo"
- "Peço a gentileza"
- "As informações contidas nesta mensagem"
- "The information contained in this message"
- "O envio mensal dos extratos bancários é fundamental"
- "Fico no aguardo do seu retorno"

Garante que o Claude veja só o conteúdo relevante da mensagem.

---

## MESES PADRÃO
`_meses_alvo_padrao()` → retorna apenas o mês anterior ao atual.
- Junho → MAIO
- Julho → JUNHO
- Janeiro → DEZEMBRO

Usado apenas quando não há threads anteriores (primeiro e-mail ao cliente).
Meses pendentes de threads existentes são calculados por `calcular_meses_pendentes_por_corpo()`.

---

## APROVAÇÃO INTERATIVA (aprovar_emails)
Lista numerada de e-mails com ícone de tipo.
Opções:
- `T` → envia todos
- `1,3` ou `2-4` ou `1 3 5` → seleciona individualmente (pede confirmação)
- `C` → cancela

---

## LOG (registrar_log_sheets)
Gravado na aba "GERAL" da `PLANILHA_SITUACAO_ID`.
Colunas: ID | EMPRESA | E-MAIL | BPO | STATUS | HISTÓRICO | CONCLUÍDA? | DESCRIÇÃO
- Cabeçalho restaurado automaticamente se apagado
- Clientes sem e-mail: resumidos em UMA linha por execução
- Timestamp em horário de Manaus

---

## SIMULAÇÃO NO GOOGLE DOCS (simular_no_docs)
Gera preview antes do envio.
Corpo dos e-mails é limpo de HTML antes de exibir: `<br>` → `\n`, `html.unescape()` para `&nbsp;` etc.

---

## EXECUTÁVEL (PyInstaller)
Build: `pyinstaller --onedir --name "AKARTOS_Extratos" --noconfirm automacao_samea.py`
Pasta `dist\AKARTOS_Extratos\` contém:
- `AKARTOS_Extratos.exe`
- `EXECUTAR.bat`
- `credentials.json`
- `ramals\ramal_samea.png`
- `_internal\` (não deletar)

`token.json` gerado na 1ª execução na máquina da Sâmea (login Google OAuth).
`_BASE` detecta se está rodando como .exe ou .py (via `sys.frozen`).
Rebuild necessário a cada mudança no `.py`.

---

## BUGS CORRIGIDOS NESTA SESSÃO
1. `samea_ja_agradeceu` marcando todos como encerrados: "recebi" batia em "recebimento" do boilerplate → corrigido usando `_limpar_corpo_para_chatbot` e frases mais específicas
2. Chatbot enviando mensagens contraditórias (agradecia e depois dizia que não recebeu): adicionado `samea_ja_agradeceu` + lógica de timestamp
3. Simulação mostrando `&nbsp;` literal: adicionado `html.unescape()` e conversão de `<br>` → `\n`
4. Chatbot recebendo boilerplate como histórico: criado `_limpar_corpo_para_chatbot()`
5. Histórico passado como texto plano ao Claude: refatorado para conversa real `user`/`assistant`
6. Meses padrão incluindo FEVEREIRO indevidamente: reduzido de 3 para 1 mês (o anterior)
7. Nome de empresa digitado errado na planilha causando "E-MAIL NOVO" incorreto: explicado que `_assunto_pertence_cliente` exige match exato

---

## PENDÊNCIAS / DECISÕES FUTURAS
- Filtro de data (DATA_INICIO) para ignorar threads antigas: aguardando alinhamento com o "chefe"
- Build automático: criar `build.bat` para facilitar rebuild do .exe
- Dashboard Streamlit: adiado por Jonas ("VEREI DEIXAR PARA LÁ POR HORA")

---

## COMO RODAR
```
cd c:\solicitacaoextrato
python automacao_samea.py
```
Ou executar `dist\AKARTOS_Extratos\EXECUTAR.bat` na máquina da Sâmea.
