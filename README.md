# suno-automation

Automação de criação de músicas no **Suno AI** (modo Advanced) via Playwright.

Lê lotes de letras em Markdown, valida tudo offline, conecta a um Chrome **já
autenticado** (via CDP) e cria as músicas uma a uma — persistindo o progresso em
`json/suno_state.json` para **nunca reprocessar nem gastar crédito à toa**.

> ⚠️ **Cada crédito do Suno é irreversível.** O script foi desenhado para ser
> conservador: valida antes de abrir o browser, tira screenshot em qualquer erro,
> para após 3 erros consecutivos e nunca reenvia uma música já submetida. Comece
> **sempre** com `--dry-run`.

---

## 1. Instalação (uma vez por máquina)

```bash
git clone https://github.com/JoseGabrielRCB/suno-automation.git
cd suno-automation
pip install -r requirements.txt
```

Não é preciso `playwright install`: o script **não** abre um navegador próprio — ele
se conecta ao seu Chrome via porta de depuração.

## 2. Abrir o Chrome com depuração e logar no Suno

**Windows (PowerShell):**
```powershell
Start-Process "C:\Program Files\Google\Chrome\Application\chrome.exe" `
  -ArgumentList "--remote-debugging-port=9222","--user-data-dir=$env:LOCALAPPDATA\Google\Chrome\suno-session"
```

**Linux/macOS:**
```bash
google-chrome --remote-debugging-port=9222 --user-data-dir="$HOME/.config/chrome-suno-session"
```

Na janela que abrir, navegue até `suno.com` e **faça login**. O perfil
(`--user-data-dir`) guarda a sessão, então você só loga uma vez por máquina.

## 3. (Opcional) Apontar um workspace específico

Sem configuração, usa o workspace padrão da conta logada. Para mirar um workspace:

```bash
export SUNO_CREATE_URL="https://suno.com/create?wid=SEU_WORKSPACE_ID"   # Linux/macOS
$env:SUNO_CREATE_URL="https://suno.com/create?wid=SEU_WORKSPACE_ID"     # PowerShell
```

## 4. Preparar as letras

Coloque seus arquivos `.md` em `./musicas/`. O formato aceito está em
[`musicas/EXEMPLO.md`](musicas/EXEMPLO.md). Resumo:

- Cada música é um heading `## NN — Título` (número vira o índice de progresso).
- `**Estilos:**` e `**Letra:**` em blocos de código (```` ``` ````).
- (Opcional) `**Excluir estilos:** \`...\`` no topo vale para o arquivo todo.
- O parser também aceita os formatos antigos `### LETRA` / `### STYLE` e `**STYLE:**` inline.

Os arquivos são processados em **ordem alfabética** — o nome define a sequência.

## 5. Rodar

```bash
# 1) Sempre valide primeiro (não gasta crédito, não clica em "Create"):
python suno_agent.py --dir musicas --dry-run --batch-size 3

# 2) Execução real de um lote (processa o próximo arquivo com músicas pendentes):
python suno_agent.py --dir musicas --batch-size 25

# 3) Todos os lotes em sequência, até acabar (com trava anti-loop):
bash rodar_lotes.sh
```

---

## Como o progresso funciona

`json/suno_state.json` guarda o status de cada música (`arquivo.md::N`):

| Status | Significado |
|--------|-------------|
| `completed` | Create clicado + URL `/song/id` detectada. Confirmada no Suno. |
| `submitted_timeout` | Create clicado + crédito gasto, mas a detecção de conclusão (30 s) expirou. **A música foi gerada** — confira na biblioteca. Não reenviar. |
| `failed` | Erro **antes** do Create. Nenhum crédito gasto. Será re-tentada. |

Músicas `completed`/`submitted_timeout` são puladas automaticamente na próxima execução.

## Se um seletor quebrar (o Suno atualizou a UI)

1. Rode um `--dry-run` — ele acusa o erro sem gastar crédito e salva screenshot em `./screenshots/`.
2. Inspecione o DOM, atualize o seletor em `suno_agent.py`.
3. Rode `--dry-run` de novo até passar (`Falhas: 0`) antes de voltar à execução real.

Veja `CLAUDE.md` para os seletores confirmados e as armadilhas conhecidas da UI nova.

## Parâmetros úteis (topo do `suno_agent.py`)

| Constante | Padrão | Descrição |
|-----------|--------|-----------|
| `MIN_CREDITS` | 10 | Abaixo disso, para tudo. |
| `MAX_CONSECUTIVE_ERRORS` | 3 | Erros seguidos → parada total. |
| `COMPLETION_TIMEOUT_S` | 30 | Espera pela detecção de conclusão após o Create. |
| `CDP_ENDPOINT` | `http://localhost:9222` | Chrome com `--remote-debugging-port`. |

## Utilitários

- `validar_todos.py` — valida todos os `.md` offline (sem browser).
- `verificar_lotes.py` — confere na API do Suno quais músicas do lote já existem no workspace.

## Arquivos ignorados pelo git

`json/` (progresso), `logs/`, `screenshots/` e as letras reais (`musicas/*.md`, exceto
o exemplo) ficam fora do versionamento — são dados de execução, específicos de cada máquina.
