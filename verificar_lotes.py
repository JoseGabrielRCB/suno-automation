"""Verifica quais músicas dos arquivos .md NÃO foram produzidas no workspace do Suno.

Roda ao final do processo. Lê todos os lotes em --dir, consulta a API do workspace
do Suno (conta atual) e reporta, por lote, as músicas ausentes.

IMPORTANTE — troca de conta:
A conta atual só contém os lotes a partir de --lote-inicial (padrão 18). Lotes
anteriores foram criados em outra conta e não estão neste workspace, então são
ignorados para não gerarem "faltantes" falsas.

Pré-requisito: Chrome aberto com --remote-debugging-port=9222 e logado no Suno.
"""
import argparse
import asyncio
import json
import re
import sys
import unicodedata
from pathlib import Path

from playwright.async_api import async_playwright

# Reusa o parser/constantes do agente (tem guard __main__; importar não abre browser).
from suno_agent import parse_file, discover_files, CREATE_URL, STATE_FILE, load_state

sys.stdout.reconfigure(encoding="utf-8")

CDP_ENDPOINT = "http://localhost:9222"


def extract_wid(url: str) -> str:
    m = re.search(r"wid=([0-9a-fA-F-]+)", url)
    return m.group(1) if m else ""


def lote_num(filename: str):
    """Extrai o número do lote do nome do arquivo ('... 18 de 25.md' -> 18)."""
    m = re.search(r"(\d+)\s+de\s+\d+", filename)
    return int(m.group(1)) if m else None


def normalize(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return " ".join(s.lower().split())


# Busca todos os clips do projeto, paginando até esvaziar. Usa clip_count (da
# própria resposta) para confirmar que o fetch foi completo.
FETCH_JS = """
async (args) => {
    const [wid, token, maxPages] = args;
    const base = "https://studio-api-prod.suno.com/api/project/" + wid;
    const all = [];
    let clipCount = null;
    let page = 1;
    while (page <= maxPages) {
        const url = base + "?page=" + page
            + "&sort=max_created_at_last_updated_clip&show_trashed=false";
        const r = await fetch(url, {
            credentials: "include",
            headers: {"Authorization": "Bearer " + token}
        });
        if (!r.ok) return {error: r.status, page: page, clips: all, clip_count: clipCount};
        const j = await r.json();
        if (clipCount === null && typeof j.clip_count === "number") clipCount = j.clip_count;
        const rows = j.project_clips || j.clips || [];
        if (!rows.length) break;
        for (const row of rows) {
            const clip = row.clip || row;
            all.push({id: clip.id, title: clip.title || "", status: clip.status || ""});
        }
        page += 1;
    }
    return {error: null, pages: page - 1, clip_count: clipCount, clips: all};
}
"""


async def fetch_clips(wid: str, max_pages: int = 1000):
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(CDP_ENDPOINT)
        ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        captured = {"token": None}

        def on_request(req):
            if "studio-api-prod.suno.com" in req.url:
                auth = req.headers.get("authorization")
                if auth and auth.lower().startswith("bearer "):
                    captured["token"] = auth.split(" ", 1)[1]

        page.on("request", on_request)
        await page.goto("https://suno.com/create?wid=" + wid, wait_until="domcontentloaded")
        await page.wait_for_timeout(5000)
        if not captured["token"]:
            await page.reload(wait_until="domcontentloaded")
            await page.wait_for_timeout(5000)
        if not captured["token"]:
            return None, None
        result = await page.evaluate(FETCH_JS, [wid, captured["token"], max_pages])
        return result, captured["token"]


def main() -> int:
    ap = argparse.ArgumentParser(description="Verifica músicas ausentes no workspace do Suno.")
    ap.add_argument("--dir", default="musicas", help="Pasta com os .md (padrão: musicas).")
    ap.add_argument("--wid", default=extract_wid(CREATE_URL),
                    help="Workspace ID do Suno (padrão: o do CREATE_URL em suno_agent.py).")
    ap.add_argument("--lote-inicial", type=int, default=18,
                    help="Primeiro lote da conta atual a verificar (padrão: 18).")
    ap.add_argument("--out", default="json/relatorio_faltantes.json", help="Arquivo de saída JSON.")
    args = ap.parse_args()

    if not args.wid:
        print("ERRO: não foi possível determinar o WID. Use --wid.")
        return 2

    # 1. Arquivos do intervalo da conta atual.
    all_files = discover_files(Path(args.dir))
    files = [f for f in all_files if (lote_num(f.name) or 0) >= args.lote_inicial]
    if not files:
        print(f"Nenhum .md com lote >= {args.lote_inicial} em {args.dir}.")
        return 2
    print(f"Verificando {len(files)} lote(s) (a partir do lote {args.lote_inicial}); "
          f"ignorando lotes anteriores (conta antiga).")

    state = load_state(STATE_FILE)

    # 2. Busca a biblioteca do workspace.
    print(f"Conectando ao Chrome (CDP) e lendo workspace {args.wid}...")
    result, token = asyncio.run(fetch_clips(args.wid))
    if token is None:
        print("ERRO: não capturei o token Bearer. Chrome aberto e logado no Suno?")
        return 3
    if result.get("error"):
        print(f"ERRO da API na página {result.get('page')}: status {result['error']}.")
        return 3

    clips = result["clips"]
    clip_count = result.get("clip_count")
    print(f"Workspace: {len(clips)} clips lidos em {result['pages']} página(s) "
          f"(clip_count reportado: {clip_count}).")

    # Salvaguardas anti-falso-negativo.
    if not clips:
        print("ERRO: 0 clips retornados — possível problema de API/sessão. Abortando.")
        return 3
    if clip_count is not None and len(clips) < clip_count:
        print(f"AVISO: li {len(clips)} de {clip_count} clips — fetch pode estar incompleto. "
              f"Confira a conexão antes de confiar no resultado.")

    ws_titles = {}
    for c in clips:
        ws_titles[normalize(c["title"])] = ws_titles.get(normalize(c["title"]), 0) + 1

    # 3. Cruzamento por título normalizado (igualdade exata).
    per_lote = []
    faltantes = []
    total_songs = total_found = 0
    for f in sorted(files, key=lambda x: lote_num(x.name) or 0):
        songs = parse_file(f)
        lote = lote_num(f.name)
        found = 0
        for s in songs:
            total_songs += 1
            if normalize(s.title) in ws_titles:
                found += 1
                total_found += 1
            else:
                faltantes.append({
                    "lote": lote,
                    "indice": s.index,
                    "titulo": s.title,
                    "key": s.key,
                    "status_state": state.get(s.key, {}).get("status", "sem registro"),
                })
        per_lote.append({"lote": lote, "arquivo": f.name,
                         "total": len(songs), "encontradas": found,
                         "faltando": len(songs) - found})

    # 4. Relatório no console.
    print("\n=== COBERTURA POR LOTE ===")
    print(f"{'Lote':>5} | {'Encontradas':>11} | {'Faltando':>8} | Total")
    print("-" * 42)
    for r in per_lote:
        print(f"{r['lote']:>5} | {r['encontradas']:>11} | {r['faltando']:>8} | {r['total']}")
    print("-" * 42)
    print(f"TOTAL: {total_found} encontradas, {len(faltantes)} faltando, de {total_songs} músicas.")

    # Sanidade: se TUDO faltou, quase certamente é erro de método/conta.
    if total_found == 0:
        print("\nAVISO: nenhuma música encontrada. Verifique o WID / a conta logada — "
              "o resultado provavelmente NÃO é confiável.")

    if faltantes:
        print("\n=== MÚSICAS FALTANTES ===")
        for m in faltantes:
            print(f"  Lote {m['lote']:>2} #{m['indice']:>2} | {m['titulo']}  [{m['status_state']}]")
    else:
        print("\nNenhuma música faltante no intervalo verificado. Tudo produzido.")

    # 5. Arquivo JSON.
    report = {
        "wid": args.wid,
        "lote_inicial": args.lote_inicial,
        "workspace_clips": len(clips),
        "clip_count_reportado": clip_count,
        "total_musicas_verificadas": total_songs,
        "total_encontradas": total_found,
        "total_faltando": len(faltantes),
        "cobertura_por_lote": per_lote,
        "faltantes": faltantes,
    }
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nRelatório salvo em {args.out}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
