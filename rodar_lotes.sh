#!/usr/bin/env bash
# Roda os lotes em sequência até concluir. Um lote (arquivo .md) por iteração.
# Para automaticamente se um lote não avançar (proteção contra lote quebrado em loop).
#
# Uso:  bash rodar_lotes.sh
cd "$(dirname "$0")"
LOG="logs/run_$(date +%Y%m%d_%H%M%S).log"
STATE="json/suno_state.json"
mkdir -p logs

pend() {  # nº de músicas ainda pendentes (não completed/submitted_timeout)
  PYTHONIOENCODING=utf-8 PYTHONUTF8=1 python -c "
import json, glob
from pathlib import Path
import suno_agent as A
try:
    d = json.load(open('$STATE', encoding='utf-8'))
except Exception:
    d = {}
p = 0
for f in glob.glob('musicas/*.md'):
    for s in A.parse_file(Path(f)):
        if d.get(s.key, {}).get('status') not in ('completed', 'submitted_timeout'):
            p += 1
print(p)
"
}

for i in $(seq 1 100); do
  before=$(pend)
  echo "=== Iteração $i | pendentes antes: $before ===" >> "$LOG"
  [ "$before" -eq 0 ] && { echo "=== TUDO CONCLUÍDO ===" >> "$LOG"; break; }
  PYTHONIOENCODING=utf-8 PYTHONUTF8=1 python suno_agent.py --dir musicas --batch-size 25 >> "$LOG" 2>&1
  after=$(pend)
  echo "=== pendentes depois: $after ===" >> "$LOG"
  if [ "$after" -ge "$before" ]; then
    echo "!!! SEM PROGRESSO ($before -> $after) — parando para diagnóstico. Veja screenshots/." >> "$LOG"
    exit 1
  fi
done
echo "FIM. Log completo em: $LOG"
