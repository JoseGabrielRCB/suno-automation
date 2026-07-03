# Operação Autônoma — Agente Suno AI

## Pré-requisitos (configuração única)

### 1. Abrir Chrome com debugging

```powershell
Start-Process "C:\Program Files\Google\Chrome\Application\chrome.exe" `
  -ArgumentList "--remote-debugging-port=9222","--user-data-dir=$env:LOCALAPPDATA\Google\Chrome\suno-session"
```

Após abrir, navegue até `suno.com` e confirme que está logado antes de rodar o script.

### 2. Instalar dependências Python

```powershell
cd caminho\para\suno-automation
pip install -r requirements.txt
```

---

## Checklist antes de cada execução

```
[ ] Chrome aberto com --remote-debugging-port=9222
[ ] suno.com carregado e logado na sessão do Chrome
[ ] Créditos suficientes no Suno (mínimo 10 por música do lote)
[ ] suno_state.json existe no diretório (pode estar vazio — normal)
[ ] Arquivos .md estão em ./musicas/
```

---

## Fluxo de execução recomendado

### Passo 1 — Dry-run (sempre antes de lote novo ou após longa pausa)

```powershell
python suno_agent.py --dir musicas --dry-run --batch-size 3
```

Resultado esperado: `Dry-run: 3 | Falhas: 0`

Só prossiga se não houver falhas. Se houver, verifique os screenshots em `./screenshots/`.

### Passo 2 — Execução real do lote

```powershell
python suno_agent.py --dir musicas --batch-size 25
```

O script seleciona automaticamente o próximo arquivo `.md` com músicas ainda não enviadas.

### Passo 3 — Repetir para cada lote

Rode o mesmo comando do Passo 2 novamente. O `suno_state.json` guarda o progresso — músicas já concluídas são puladas automaticamente e o script avança para o próximo arquivo `.md`.

---

## Adicionando novos arquivos de música

Coloque os novos `.md` em `./musicas/`. O script processa os arquivos em **ordem alfabética** — o nome do arquivo define a sequência de execução.

Mantenha o padrão de nomenclatura existente para que a ordem seja preservada:

```
2026 _ Rádio da Sorte _ Gospel Popular _ 01 de 25.md
2026 _ Rádio da Sorte _ Gospel Popular _ 02 de 25.md
...
2026 _ Rádio da Sorte _ Gospel Popular _ 09 de 25.md   ← novo arquivo
```

### Formato obrigatório dentro do .md

O parser identifica músicas pelo emoji 🎵 no heading `##`. Qualquer heading `##` sem esse padrão é ignorado.

```markdown
## 🎵 1 — Título da Música

### LETRA
[letra completa aqui]

### STYLE
[style aqui]

---

## 🎵 2 — Próxima Música

### LETRA
...

### STYLE
...
```

**Limites de campo:**

| Campo | Limite |
|-------|--------|
| Título | 200 caracteres |
| Style | 1.000 caracteres |
| Letra | 5.000 caracteres |

Se o arquivo não seguir esse formato, o parser não encontra as músicas e o lote é pulado com erro de validação — sem gastar créditos.

---

## Recuperação de falhas

| Situação | O que fazer |
|----------|-------------|
| Script parou com `3 erros consecutivos` | Verifique `./screenshots/` — geralmente seletor quebrado ou Chrome fechado |
| Música com status `failed` no state | Será re-tentada automaticamente na próxima execução |
| Música com `submitted_timeout` | Já foi enviada ao Suno — verifique na biblioteca. **Não re-submeter** |
| Chrome fechou durante execução | Reabra, faça login, rode novamente — o state evita duplicatas |
| Créditos insuficientes (`< 10`) | Script para automaticamente com erro `StopAll` |
| Seletor quebrou após update do Suno | Inspecione o DOM, atualize o seletor em `suno_agent.py`, faça dry-run antes de retomar |

---

## Arquivos importantes

| Arquivo | Função |
|---------|--------|
| `suno_agent.py` | Script principal de automação |
| `suno_state.json` | Progresso persistido (não apagar) |
| `./musicas/*.md` | Arquivos de entrada com letras e styles |
| `./screenshots/` | Screenshots de erro para diagnóstico |
| `CLAUDE.md` | Regras de operação do agente |

---

## Interpretando os status no suno_state.json

| Status | Significado |
|--------|-------------|
| `completed` | Create clicado + URL /song/id detectada. Música confirmada no Suno. |
| `submitted_timeout` | Create clicado + crédito gasto, mas a detecção de conclusão expirou. Música foi gerada — verifique na biblioteca do Suno. |
| `failed` | Erro antes do clique em Create. Nenhum crédito gasto. Será re-tentada. |
| `dry_run` | Processada em modo dry-run. Será processada normalmente na execução real. |

---

## Parâmetros configuráveis em suno_agent.py

| Constante | Valor padrão | Descrição |
|-----------|-------------|-----------|
| `COMPLETION_TIMEOUT_S` | 30 | Segundos aguardando detecção de conclusão após Create |
| `BETWEEN_SONGS_MS` | 2000 | Pausa em ms entre músicas |
| `HYDRATION_MS` | 1500 | Espera de hidratação React após navegar |
| `MIN_CREDITS` | 10 | Créditos mínimos — abaixo disso, para tudo |
| `MAX_CONSECUTIVE_ERRORS` | 3 | Erros seguidos antes de parar o lote |
| `CREATE_URL` | `https://suno.com/create?wid=...` | URL do workspace de criação |
