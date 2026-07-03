# LOTE 00 — Exemplo (mostra o formato aceito pelo parser)

## ⚙️ CONFIGURAÇÃO FIXA DO LOTE
> Bloco opcional. A linha "Excluir estilos" abaixo vale para TODAS as músicas do arquivo
> e é preenchida no campo "Exclude styles" do Suno (dentro de "More Options").

- **Excluir estilos:** `samba, funk, trap, conteúdo explícito`

---

## 01 — Título da Primeira Música
`comentário livre nesta linha é ignorado pelo parser`

**Estilos:**
```
pop acústico, violão, voz masculina, animado, Brazilian Portuguese, 100 BPM
```

**Letra:**
```
[Intro]

[Verse]
Escreva aqui a primeira estrofe
Uma linha por verso, como no Suno

[Chorus]
O refrão da música vem aqui
Com o gancho que gruda no ouvido

[Outro]
Fim da música
```

---

## 02 — Título da Segunda Música

**Estilos:**
```
sertanejo, viola caipira, voz feminina, romântico, Brazilian Portuguese, 95 BPM
```

**Letra:**
```
[Verse]
Cada música é delimitada por um heading "## NN — Título"
O número (NN) vira o índice usado no controle de progresso

[Chorus]
Estilos e Letra vão dentro de blocos de código (```)
Nada de créditos é gasto até você rodar sem --dry-run
```
