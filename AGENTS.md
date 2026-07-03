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
- Title:          input[placeholder="Song Title (Optional)"]
- Style:          textarea[maxlength="1000"]
- Lyrics:         textarea[data-testid="lyrics-textarea"]
- Create:         button[aria-label="Create song"]
- Credits:        button[aria-label*="Credits remaining"]

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
