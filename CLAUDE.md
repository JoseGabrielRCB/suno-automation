# Agente Suno AI — Regras de Operação

## Contexto

Este projeto automatiza a criação de músicas no Suno AI via Playwright.
Cada crédito é irreversível. Erros que gastem crédito sem resultado são inaceitáveis.

## Regras absolutas

1. NUNCA resubmeter uma música sem evidência de falha pré-submit
2. NUNCA continuar após 3 erros consecutivos — parar e reportar
3. SEMPRE ler suno_state.json antes de qualquer execução
4. SEMPRE validar todos os .md antes de abrir o browser
5. SEMPRE capturar screenshot em qualquer erro

## Seletores confirmados (não alterar sem dry-run de validação)

- Advanced mode:  button[aria-label="Advanced"]
- Title:          input[placeholder="Song Title (Optional)"]  (ver nota UI nova)
- Style:          textarea[maxlength="1000"]
- Lyrics:         div.lyrics-editor-content  (contenteditable; ver nota UI nova)
- Exclude styles: input[placeholder="Exclude styles"] (dentro de "More Options")
- Create:         button[aria-label="Create song"]
- Credits:        button[aria-label*="Credits remaining"]

Modo de letra (Write/Prompt/Instrumental): o editor de letra só aparece em "Write".
`ensure_write_mode()` clica "Write" (agora `<button role="radio">`; clique NATIVO)
antes de escrever a letra. Em "Instrumental" o campo some do DOM; em "Prompt" há uma
textarea (maxlength=3000) que gera letra automática — não usar.

### UI nova do Suno (jul/2026) — três armadilhas confirmadas e corrigidas

1. **Letra virou contenteditable** (`div.lyrics-editor-content`), não mais textarea.
   Ignora o setter de `value`; preencher com `insert_text` (função `fill_lyrics`).
   NÃO usar Ctrl+A/Delete para limpar — num editor vazio o select-all vaza e apaga
   o título.
2. **Título tem DOIS inputs espelhados** (mesmo estado React). O setter sintético não
   fixa (React reverte) — usar `.fill()` NATIVO em `:visible` (função `fill_title`).
3. **Ordem importa**: inserir a letra RE-RENDERIZA o form e ZERA o título. Por isso o
   título é preenchido POR ÚLTIMO, depois da letra. Ordem em `process_song`:
   style → exclude → write_mode → lyrics → **title**.

## Limites de campo

- titulo: 200 chars (conservador)
- estilo: 1.000 chars
- letra:  5.000 chars

## Chrome

Conectar via: playwright.chromium.connect_over_cdp("<http://localhost:9222>")
Não usar extensão. Não fazer login. Reutilizar sessão existente.

## Se um seletor quebrar

1. Inspecionar DOM via javascript_tool ou DevTools
2. Identificar novo seletor (preferência: data-testid > aria-label > placeholder)
3. Atualizar suno_agent.py
4. Rodar dry-run com 1 arquivo
5. Só retomar lote após confirmação
