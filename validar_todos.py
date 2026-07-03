import sys
sys.path.insert(0, '.')
from pathlib import Path
from suno_agent import parse_file, validate_song, TITLE_MAXLEN, STYLE_MAXLEN, LYRICS_MAXLEN

musicas_dir = Path('musicas')
files = sorted(musicas_dir.glob('*.md'))

total_songs = 0
total_errors = 0
erros_detalhados = []

for f in files:
    songs = parse_file(f)
    for s in songs:
        total_songs += 1
        errs = validate_song(s)
        if errs:
            total_errors += 1
            erros_detalhados.append((f.name, s.index, s.title, len(s.lyrics), len(s.style), errs))

print(f"Arquivos : {len(files)}")
print(f"Musicas  : {total_songs}")
print(f"Erros    : {total_errors}")
print()

# Contagem por arquivo
print("Contagem por lote:")
for f in files:
    songs = parse_file(f)
    count = len(songs)
    flag = " <<<" if count != 25 else ""
    print(f"  {count:2d} musicas  {f.name}{flag}")

print()
if erros_detalhados:
    print("ERROS ENCONTRADOS:")
    for fname, idx, title, llen, slen, errs in erros_detalhados:
        print(f"  [{fname}] #{idx} \"{title}\"")
        print(f"    letra={llen} style={slen} -> {errs}")
else:
    print("Todos os arquivos OK - nenhum erro encontrado.")
