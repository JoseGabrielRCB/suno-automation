#!/usr/bin/env python3
"""
suno_agent.py — Automação de criação de músicas no Suno AI (modo Advanced).

Lê lotes de letras em Markdown (pasta --dir), valida tudo offline, conecta a um
Chrome já autenticado via CDP e cria as músicas uma a uma, persistindo progresso
em suno_state.json para nunca reprocessar nem gastar crédito à toa.

Uso:
    python suno_agent.py --dir musicas --dry-run
    python suno_agent.py --dir musicas --batch-size 1
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    import winsound  # Windows: beep nativo para alertar sobre captcha
except Exception:
    winsound = None

import structlog
from pydantic import BaseModel
from rich.console import Console
from rich.table import Table
from rich.progress import (
    Progress,
    SpinnerColumn,
    BarColumn,
    TextColumn,
    TimeElapsedColumn,
    MofNCompleteColumn,
)
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)
from playwright.sync_api import (
    sync_playwright,
    Page,
    Error as PlaywrightError,
)

# ───────────────────────────────────────────────────────────────────────────
# CONFIGURAÇÃO GLOBAL — edite aqui livremente
# ───────────────────────────────────────────────────────────────────────────

# URL usada no page.goto. Configure por variável de ambiente para não ter de editar
# o código em cada máquina:
#   SUNO_CREATE_URL="https://suno.com/create?wid=<seu_workspace_id>"
# Sem a variável, usa o workspace padrão da conta logada no Chrome (sem wid).
CREATE_URL = os.environ.get("SUNO_CREATE_URL", "https://suno.com/create")

CDP_ENDPOINT = "http://localhost:9222"  # Chrome com --remote-debugging-port=9222
MIN_CREDITS = 10                        # abaixo disso, para tudo
HYDRATION_MS = 1500                     # espera de hidratação do React após navegar
BETWEEN_SONGS_MS = 2000                 # pausa entre músicas (execução real)
POLL_INTERVAL_S = 3                     # intervalo de polling da conclusão
COMPLETION_TIMEOUT_S = 30               # tempo máximo aguardando conclusão
MAX_CONSECUTIVE_ERRORS = 3              # erros seguidos → parada total

# reCAPTCHA / hCaptcha / Turnstile — detecção + alerta sonoro
CAPTCHA_WAIT_TIMEOUT_S = 180   # tempo máx. aguardando o usuário resolver o captcha
CAPTCHA_POLL_S = 3             # intervalo entre verificações/buzz enquanto aguarda
CAPTCHA_MIN_SIZE = 60          # altura mín. (px) do iframe p/ contar como desafio visível
CAPTCHA_BUZZ_FREQS = (1000, 1400)  # frequências (Hz) do beep de alerta

# Limites de campo confirmados no DOM
TITLE_MAXLEN = 200    # sem maxlength no DOM — limite conservador
STYLE_MAXLEN = 1000   # hard limit
LYRICS_MAXLEN = 5000  # hard limit

# Seletores confirmados no DOM
SEL_ADVANCED = 'button[aria-label="Advanced"]'
SEL_TITLE = 'input[placeholder="Song Title (Optional)"]'
SEL_STYLE = 'textarea[maxlength="1000"]'
# Suno trocou a <textarea data-testid="lyrics-textarea"> por um editor rich-text
# contenteditable (UI nova, jul/2026). O campo de letra agora é este DIV; ele NÃO
# aceita o setter de 'value' — ver fill_lyrics(). O placeholder "Start writing
# lyrics…" fica num div irmão (.lyrics-editor-placeholder) e some ao digitar.
SEL_LYRICS = 'div.lyrics-editor-content'
SEL_CREATE = 'button[aria-label="Create song"]'
SEL_CREDITS = 'button[aria-label*="Credits remaining"]'
SEL_EXCLUDE = 'input[placeholder="Exclude styles"]'  # dentro de "More Options"

STATE_FILE = Path("json/suno_state.json")
SCREENSHOTS_DIR = Path("screenshots")
DONE_STATUSES = {"completed", "submitted_timeout"}

# ───────────────────────────────────────────────────────────────────────────
# LOGGING (structlog → JSON em stderr, deixando o stdout livre p/ a UI rich)
# ───────────────────────────────────────────────────────────────────────────
structlog.configure(
    processors=[
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
    logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
    cache_logger_on_first_use=True,
)
log = structlog.get_logger()

# Windows: força UTF-8 no console para acentos e o emoji 🎵 renderizarem certo.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

console = Console()

# ───────────────────────────────────────────────────────────────────────────
# MODELO
# ───────────────────────────────────────────────────────────────────────────


class Song(BaseModel):
    source_file: str
    index: int
    title: str
    style: str
    lyrics: str
    exclude_styles: str = ""  # estilos a excluir (campo "Exclude styles" do Suno)

    @property
    def key(self) -> str:
        # Identificador estável p/ o estado: arquivo + número da música no lote.
        return f"{self.source_file}::{self.index}"


def validate_song(song: Song) -> list[str]:
    """Retorna lista de problemas; vazia = válida."""
    errs: list[str] = []
    if not song.title.strip():
        errs.append("título vazio")
    if not song.style.strip():
        errs.append("style vazio")
    if not song.lyrics.strip():
        errs.append("letra vazia")
    if len(song.title) > TITLE_MAXLEN:
        errs.append(f"título {len(song.title)}>{TITLE_MAXLEN}")
    if len(song.style) > STYLE_MAXLEN:
        errs.append(f"style {len(song.style)}>{STYLE_MAXLEN}")
    if len(song.lyrics) > LYRICS_MAXLEN:
        errs.append(f"letra {len(song.lyrics)}>{LYRICS_MAXLEN}")
    return errs


# ───────────────────────────────────────────────────────────────────────────
# PARSER MARKDOWN
# ───────────────────────────────────────────────────────────────────────────

# Qualquer heading nível 2 (delimita blocos). Não casa "### LETRA" (3 hashes)
# nem o título nível 1 do documento.
HEADING_ANY = re.compile(r"^##\s.*$", re.MULTILINE)
# Heading de música: "## 🎵 1 — Título" ou "## 01 — Título" (emoji opcional,
# número pode ter zero à esquerda). Aceita em-dash, en-dash ou hífen.
SONG_HEADING = re.compile(r"^##\s*(?:🎵\s*)?(\d+)\s*[—\-–.]\s*(.+?)\s*$")
LETRA_RE = re.compile(r"^###\s*LETRA\s*$", re.MULTILINE)
STYLE_RE = re.compile(r"^###\s*STYLE\s*$", re.MULTILINE)
# Formato novo (Forró): **STYLE:** na mesma linha; letra começa no primeiro [Tag]
STYLE_INLINE_RE = re.compile(r"^\*\*STYLE:\*\*\s*(.+?)$", re.MULTILINE)
LYRICS_SECTION_RE = re.compile(r"^\[", re.MULTILINE)
# Formato Tchê Music: **Estilos:** / **Letra:** com conteúdo em code fence (```).
# [^\n]* absorve um identificador de linguagem opcional após a cerca de abertura.
ESTILOS_FENCE_RE = re.compile(r"\*\*Estilos:\*\*\s*```[^\n]*\n(.*?)\n```", re.DOTALL)
LETRA_FENCE_RE = re.compile(r"\*\*Letra:\*\*\s*```[^\n]*\n(.*?)\n```", re.DOTALL)
# Config fixa do lote: "- **Excluir estilos:** `samba, pagode, ...`" (nível de arquivo,
# vale para todas as músicas do lote). Aceita conteúdo em crase ou texto solto.
EXCLUDE_STYLES_RE = re.compile(r"\*\*Excluir estilos:\*\*\s*`?([^`\n]+?)`?\s*$", re.MULTILINE)


def _between(block: str, start_re: re.Pattern, end_re: Optional[re.Pattern]) -> str:
    s = start_re.search(block)
    if not s:
        return ""
    start = s.end()
    if end_re is not None:
        e = end_re.search(block, start)
        end = e.start() if e else len(block)
    else:
        end = len(block)
    return block[start:end]


def _clean_style(text: str) -> str:
    text = text.strip()
    # Remove um separador horizontal final ("---") que precede a próxima música.
    text = re.sub(r"\n+-{3,}\s*$", "", text).strip()
    return text


def parse_file(path: Path) -> list[Song]:
    """Extrai todas as músicas (## 🎵) de um arquivo de lote.

    O bloco de metadados (tabela | Campo | Valor | OU linha **METADADOS:**) fica
    entre o heading e ### LETRA e é simplesmente ignorado — só Title/LETRA/STYLE
    importam para o Suno.
    """
    text = path.read_text(encoding="utf-8")
    # Config fixa do lote (vale para todas as músicas): "Excluir estilos".
    ex_m = EXCLUDE_STYLES_RE.search(text)
    exclude_styles = ex_m.group(1).strip() if ex_m else ""
    headings = list(HEADING_ANY.finditer(text))
    songs: list[Song] = []
    for i, h in enumerate(headings):
        m = SONG_HEADING.match(h.group(0))
        if not m:
            # Headings não-música (ex.: "## ✅ FECHAMENTO") apenas delimitam.
            continue
        start = h.end()
        end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
        block = text[start:end]
        estilos_fence = ESTILOS_FENCE_RE.search(block)
        letra_fence = LETRA_FENCE_RE.search(block)
        if LETRA_RE.search(block) and STYLE_RE.search(block):
            # Formato antigo: ### LETRA / ### STYLE
            lyrics = _between(block, LETRA_RE, STYLE_RE).strip()
            style = _clean_style(_between(block, STYLE_RE, None))
        elif estilos_fence and letra_fence:
            # Formato Tchê Music: **Estilos:** / **Letra:** em code fences (```)
            style = estilos_fence.group(1).strip()
            lyrics = letra_fence.group(1).strip()
        else:
            # Formato novo (Forró): letra começa no primeiro [Tag]; **STYLE:** inline
            style_m = STYLE_INLINE_RE.search(block)
            lyrics_start_m = LYRICS_SECTION_RE.search(block)
            style = style_m.group(1).strip() if style_m else ""
            if lyrics_start_m and style_m:
                lyrics = block[lyrics_start_m.start():style_m.start()].strip()
            else:
                lyrics = ""
        songs.append(
            Song(
                source_file=path.name,
                index=int(m.group(1)),
                title=m.group(2).strip(),
                style=style,
                lyrics=lyrics,
                exclude_styles=exclude_styles,
            )
        )
    return songs


# ───────────────────────────────────────────────────────────────────────────
# ESTADO (persistência atômica)
# ───────────────────────────────────────────────────────────────────────────


def load_state(path: Path) -> dict:
    if path.exists() and path.stat().st_size > 0:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.warning("state_corrompido_ignorado", path=str(path))
    return {}


def save_state(path: Path, state: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)  # replace atômico no mesmo filesystem


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ───────────────────────────────────────────────────────────────────────────
# EXCEÇÕES DE CONTROLE
# ───────────────────────────────────────────────────────────────────────────


class PreSubmitError(Exception):
    """Falha ANTES do clique em Create — nenhum crédito gasto."""


class StopAll(Exception):
    """Condição de parada total (créditos insuficientes, erros seguidos)."""


# ───────────────────────────────────────────────────────────────────────────
# BROWSER / PLAYWRIGHT
# ───────────────────────────────────────────────────────────────────────────

# Padrão React para componentes controlados: atribuir via o setter nativo do
# prototype e disparar 'input'/'change' faz o React perceber a mudança.
JS_SET_VALUE = """
([selector, value]) => {
  // Prefere o primeiro elemento visível (visibility !== 'hidden' e display !== 'none').
  const all = Array.from(document.querySelectorAll(selector));
  const el = all.find(e => {
    const s = window.getComputedStyle(e);
    return s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0';
  }) || all[0];
  if (!el) return false;
  const proto = el.tagName === 'TEXTAREA'
    ? window.HTMLTextAreaElement.prototype
    : window.HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
  setter.call(el, value);
  el.dispatchEvent(new Event('input', {bubbles: true}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
  return true;
}
"""

JS_AUDIO_SRC = """
() => {
  const a = document.querySelector('audio');
  return a && a.src && a.src.length > 0 ? a.src : null;
}
"""

# Detecta um desafio de captcha VISÍVEL (reCAPTCHA challenge, hCaptcha, Turnstile).
# Filtra por visibilidade + altura mínima para ignorar iframes invisíveis e o
# badge do reCAPTCHA v3 (que não exige interação).
JS_CAPTCHA_PRESENT = """
(minH) => {
  const vis = (el) => {
    const s = window.getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return s.display !== 'none' && s.visibility !== 'hidden'
        && parseFloat(s.opacity || '1') > 0.1
        && r.width > 40 && r.height >= minH
        && r.bottom > 0 && r.top < (window.innerHeight || 1080);
  };
  for (const f of document.querySelectorAll('iframe')) {
    const src = (f.getAttribute('src') || '').toLowerCase();
    const title = (f.getAttribute('title') || '').toLowerCase();
    const isHcaptcha = src.includes('hcaptcha') || title.includes('hcaptcha');
    const isRecaptchaChallenge = src.includes('bframe') || title.includes('challenge');
    const isTurnstile = src.includes('challenges.cloudflare.com') || src.includes('turnstile');
    if ((isHcaptcha || isRecaptchaChallenge || isTurnstile) && vis(f)) return true;
  }
  for (const sel of ['.h-captcha', '#challenge-stage', '[id^="cf-chl"]']) {
    const el = document.querySelector(sel);
    if (el && vis(el)) return true;
  }
  return false;
}
"""

UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                     r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def apply_stealth(page: Page) -> None:
    """Aplica playwright-stealth em best-effort (API varia entre versões)."""
    try:
        from playwright_stealth import Stealth  # versões novas
        Stealth().apply_stealth_sync(page)
        return
    except Exception:
        pass
    try:
        from playwright_stealth import stealth_sync  # versões antigas
        stealth_sync(page)
        return
    except Exception as e:
        log.info("stealth_indisponivel", error=str(e))


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=8),  # ~2s, 4s, 8s
    retry=retry_if_exception_type(PlaywrightError),
    reraise=True,
)
def navigate(page: Page) -> None:
    page.goto(CREATE_URL, wait_until="domcontentloaded")


def ensure_advanced(page: Page) -> None:
    """Garante o modo Advanced ativo (campos separados disponíveis)."""
    btn = page.wait_for_selector(SEL_ADVANCED, timeout=15000)
    if btn is None:
        raise PreSubmitError("botão Advanced não encontrado")
    # Verifica pela classe CSS 'active' — mais confiável que verificar SEL_LYRICS,
    # pois o Suno v5.5 mantém a lyrics-textarea no DOM mesmo em modo Simple.
    is_active = page.evaluate(
        "(sel) => { const b = document.querySelector(sel); return b ? b.classList.contains('active') : false; }",
        SEL_ADVANCED,
    )
    if not is_active:
        btn.click()
        # Confirma que ficou ativo pela textarea de Style (maxlength=1000), sempre
        # presente no Advanced — a lyrics-textarea some quando o modo de letra está
        # em "Instrumental"/"Prompt", então não serve para confirmar o Advanced.
        try:
            page.wait_for_selector(f"{SEL_STYLE}:visible", timeout=10000)
        except PlaywrightError:
            raise PreSubmitError("modo Advanced não ativou (campo Style ausente)")


def ensure_write_mode(page: Page) -> None:
    """Garante o modo de letra 'Write' (letra manual escrita por nós).

    O seletor de letra tem 3 modos: Write / Prompt / Instrumental. Em
    'Instrumental' a lyrics-textarea é REMOVIDA do DOM; em 'Prompt' o campo visível
    é outra textarea (maxlength=3000) que gera letra automática. Em ambos os casos,
    submeter geraria uma música errada — gasto de crédito irreversível. Aqui
    clicamos 'Write' (clique NATIVO — o .click() via JS não dispara o React) e
    aguardamos a lyrics-textarea (data-testid) reaparecer visível.
    """
    # Já visível → 'Write' já está ativo, nada a fazer.
    try:
        page.wait_for_selector(f"{SEL_LYRICS}:visible", timeout=2000)
        return
    except PlaywrightError:
        pass
    # Clica a aba "Write". Na UI nova (jul/2026) é um <button role="radio"> com <span>
    # "Write" dentro — por isso role="radio" vem primeiro; texto/button como fallback.
    clicked = False
    for make_locator in (
        lambda: page.get_by_role("radio", name="Write", exact=True),
        lambda: page.get_by_text("Write", exact=True),
        lambda: page.get_by_role("button", name="Write", exact=True),
    ):
        try:
            make_locator().first.click(timeout=4000)
            clicked = True
            break
        except PlaywrightError:
            continue
    if not clicked:
        raise PreSubmitError("aba 'Write' do seletor de letra não encontrada")
    try:
        page.wait_for_selector(f"{SEL_LYRICS}:visible", timeout=8000)
    except PlaywrightError:
        raise PreSubmitError("lyrics-textarea não apareceu após selecionar 'Write'")


def fill_field(page: Page, selector: str, value: str, name: str) -> None:
    try:
        # ":visible" é pseudo-classe do Playwright — ignora elementos com visibility:hidden
        page.wait_for_selector(f"{selector}:visible", timeout=15000)
    except PlaywrightError:
        raise PreSubmitError(f"campo {name} não encontrado ({selector})")
    ok = page.evaluate(JS_SET_VALUE, [selector, value])
    if not ok:
        raise PreSubmitError(f"falha ao preencher campo {name}")


def fill_title(page: Page, title: str) -> None:
    """Preenche o título (input). UI nova (jul/2026): há DOIS inputs
    'Song Title (Optional)' espelhados pelo MESMO estado React — o setter sintético
    (JS_SET_VALUE) preenche um deles mas o React reverte. O .fill() NATIVO do
    Playwright dispara os eventos corretos e o valor propaga para ambos e fixa.
    Erro aqui é pré-submit (nenhum crédito gasto).
    """
    loc = page.locator(f"{SEL_TITLE}:visible").first
    try:
        loc.fill(title, timeout=15000)
    except PlaywrightError:
        raise PreSubmitError(f"campo title não preenchível ({SEL_TITLE})")


def fill_lyrics(page: Page, lyrics: str) -> None:
    """Preenche o editor de letra (contenteditable, UI nova do Suno — jul/2026).

    A antiga <textarea data-testid="lyrics-textarea"> virou um editor rich-text
    (div.lyrics-editor-content). Editores contenteditable IGNORAM o setter de
    'value' (JS_SET_VALUE), por isso o texto precisa entrar como input nativo:
    click → Ctrl+A/Delete (limpa) → insert_text, que dispara 'insertText' — o
    editor do Suno reconhece, preservando quebras de linha e as tags [Refrão].
    Erro aqui é pré-submit (nenhum crédito gasto).
    """
    try:
        page.wait_for_selector(f"{SEL_LYRICS}:visible", timeout=15000)
    except PlaywrightError:
        raise PreSubmitError(f"editor de letra não encontrado ({SEL_LYRICS})")
    editor = page.locator(SEL_LYRICS).first
    editor.click()
    # NÃO usar Ctrl+A/Delete para "limpar": num contenteditable vazio o select-all
    # vaza para a página e o Delete apagaria o título já preenchido. Cada música
    # navega em página NOVA (process_song chama navigate()), então o editor já está
    # vazio — basta inserir. insert_text respeita as quebras de linha e tags [Refrão].
    page.keyboard.insert_text(lyrics)
    page.wait_for_timeout(200)  # deixa o editor propagar o estado ao React
    # contenteditable não tem .value — confere pelo innerText que o texto entrou
    filled_len = page.evaluate(
        "(sel) => { const e = document.querySelector(sel); return e ? e.innerText.length : 0; }",
        SEL_LYRICS,
    )
    if filled_len == 0:
        raise PreSubmitError("editor de letra permaneceu vazio após inserção")


def fill_exclude_styles(page: Page, value: str) -> None:
    """Preenche o campo 'Exclude styles' (dentro de 'More Options').

    Se o lote não define exclusões, é no-op. O campo costuma ficar visível no
    Advanced, mas pode estar dentro de 'More Options' recolhido — nesse caso
    expandimos antes. Erro aqui é pré-submit (nenhum crédito gasto).
    """
    if not value.strip():
        return
    if page.query_selector(f"{SEL_EXCLUDE}:visible") is None:
        # Tenta expandir 'More Options' (o DIV com texto exato do painel de criação).
        try:
            page.get_by_text("More Options", exact=True).first.click(timeout=3000)
            page.wait_for_selector(f"{SEL_EXCLUDE}:visible", timeout=5000)
        except PlaywrightError:
            raise PreSubmitError("campo 'Exclude styles' não encontrado (More Options)")
    ok = page.evaluate(JS_SET_VALUE, [SEL_EXCLUDE, value])
    if not ok:
        raise PreSubmitError("falha ao preencher 'Exclude styles'")


def create_enabled(page: Page) -> bool:
    btn = page.query_selector(SEL_CREATE)
    if btn is None:
        return False
    return not btn.is_disabled()


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=8),
    retry=retry_if_exception_type(PlaywrightError),
    reraise=True,
)
def read_credits(page: Page) -> Optional[int]:
    btn = page.query_selector(SEL_CREDITS)
    if btn is None:
        return None
    label = btn.get_attribute("aria-label") or ""
    m = re.search(r"([\d.,]+)\s*Credits remaining", label, re.I) or re.search(r"(\d[\d.,]*)", label)
    if not m:
        return None
    return int(re.sub(r"[.,]", "", m.group(1)))


def wait_for_completion(page: Page) -> tuple[str, Optional[str]]:
    """Polling: sucesso se a URL virar /song/<id> OU houver <audio> com src.

    Retorna ("completed", song_id) ou ("submitted_timeout", None).
    """
    deadline = time.time() + COMPLETION_TIMEOUT_S
    while time.time() < deadline:
        m = re.search(r"/song/([0-9a-zA-Z-]+)", page.url)
        if m:
            return "completed", m.group(1)
        try:
            src = page.evaluate(JS_AUDIO_SRC)
        except PlaywrightError:
            src = None
        if src:
            mm = UUID_RE.search(src)
            return "completed", (mm.group(0) if mm else None)
        page.wait_for_timeout(POLL_INTERVAL_S * 1000)
    return "submitted_timeout", None


def screenshot(page: Page, tag: str) -> None:
    SCREENSHOTS_DIR.mkdir(exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", tag)
    path = SCREENSHOTS_DIR / f"{safe}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
    try:
        page.screenshot(path=str(path))
        log.info("screenshot", path=str(path))
    except Exception as e:  # screenshot nunca deve derrubar o fluxo
        log.warning("screenshot_falhou", error=str(e))


def buzz(repeats: int = 2) -> None:
    """Alerta sonoro. Tenta winsound.Beep → MessageBeep → PowerShell → bell."""
    for _ in range(repeats):
        played = False
        if winsound is not None:
            try:
                for freq in CAPTCHA_BUZZ_FREQS:
                    winsound.Beep(int(freq), 300)
                played = True
            except Exception:
                pass
        if not played and winsound is not None:
            # Beep() falha em PCs sem PC speaker — MessageBeep usa o mixer de áudio
            try:
                winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
                played = True
            except Exception:
                pass
        if not played:
            # Último recurso: PowerShell [console]::beep() funciona mesmo em background
            try:
                import subprocess
                freqs = " ".join(
                    f"[console]::beep({f},300)" for f in CAPTCHA_BUZZ_FREQS
                )
                subprocess.run(
                    ["powershell", "-NoProfile", "-c", freqs],
                    capture_output=True, timeout=3,
                )
                played = True
            except Exception:
                pass
        if not played:
            try:
                sys.stdout.write("\a")
                sys.stdout.flush()
            except Exception:
                pass
        time.sleep(0.3)


def captcha_present(page: Page) -> bool:
    try:
        return bool(page.evaluate(JS_CAPTCHA_PRESENT, CAPTCHA_MIN_SIZE))
    except PlaywrightError:
        return False


def wait_for_captcha(page: Page, song: Song, raise_on_timeout: bool = True) -> None:
    """Se um captcha estiver visível, alerta com buzz e aguarda o usuário resolvê-lo
    no navegador, retomando sozinho assim que ele sumir.

    IMPORTANTE: após o clique em Create, chame com raise_on_timeout=False — nunca
    levantar PreSubmitError pós-submit, senão o retry re-clicaria Create e gastaria
    crédito em dobro.
    """
    if not captcha_present(page):
        return
    log.warning("captcha_detectado", song=song.key)
    console.print(
        f"[bold red]⚠ Captcha detectado em '{song.title}'. "
        f"Resolva no navegador — retomo sozinho ao terminar.[/bold red]"
    )
    screenshot(page, f"captcha_song{song.index}")
    deadline = time.time() + CAPTCHA_WAIT_TIMEOUT_S
    while time.time() < deadline:
        if not captcha_present(page):
            log.info("captcha_resolvido", song=song.key)
            console.print("[green]Captcha resolvido — retomando.[/green]")
            return
        buzz()
        page.wait_for_timeout(CAPTCHA_POLL_S * 1000)
    log.warning("captcha_timeout", song=song.key)
    if raise_on_timeout:
        raise PreSubmitError("captcha não resolvido dentro do tempo limite")


def process_song(page: Page, song: Song, dry_run: bool) -> tuple[str, Optional[str], Optional[int]]:
    """Processa uma música na ordem obrigatória do briefing.

    Retorna (status, song_id, credits). status ∈ {completed, submitted_timeout, dry_run}.
    Levanta PreSubmitError (antes do clique, sem gasto) ou StopAll (créditos).
    Importante: nenhum PreSubmitError é levantado após o clique em Create, então
    re-tentar este método nunca causa débito duplo.
    """
    navigate(page)                       # a. navegar
    page.wait_for_timeout(HYDRATION_MS)  # b. hidratação React
    ensure_advanced(page)                # c. Advanced ativo

    # d. preencher via React setter.
    # ORDEM IMPORTA (UI nova, jul/2026): a inserção no editor de letra re-renderiza o
    # formulário e ZERA o campo de título. Por isso o título é preenchido POR ÚLTIMO,
    # depois da letra — validado por dry-run que os 3 campos sobrevivem juntos.
    fill_field(page, SEL_STYLE, song.style, "style")
    fill_exclude_styles(page, song.exclude_styles)  # "Exclude styles" (More Options)
    ensure_write_mode(page)  # revela o editor de letra (modo Write) antes de escrever
    fill_lyrics(page, song.lyrics)  # contenteditable → insert_text (não JS_SET_VALUE)
    fill_title(page, song.title)  # POR ÚLTIMO: input espelhado, .fill() nativo
    page.wait_for_timeout(400)  # deixa o React propagar o estado

    # d2. captcha pode surgir durante a injeção — alerta (buzz) e aguarda resolução
    wait_for_captcha(page, song)

    # e. confirmar Create habilitado
    if not create_enabled(page):
        raise PreSubmitError("Create permaneceu disabled após preencher")

    # f. créditos
    credits = read_credits(page)
    log.info("creditos", song=song.key, credits=credits)
    if credits is not None and credits < MIN_CREDITS:
        raise StopAll(f"créditos insuficientes ({credits} < {MIN_CREDITS})")

    # g. dry-run: screenshot e pula sem clicar
    if dry_run:
        screenshot(page, f"dryrun_song{song.index}")
        return "dry_run", None, credits

    # h. clicar Create (débito ocorre AQUI, irreversível)
    log.info("create_click", song=song.key, title=song.title)
    page.click(SEL_CREATE)

    # h2. captcha pode gatekeepear a submissão — aguarda sem levantar erro
    # (pós-clique nunca levanta PreSubmitError, p/ não causar débito duplo no retry)
    wait_for_captcha(page, song, raise_on_timeout=False)

    # i. aguardar conclusão
    status, song_id = wait_for_completion(page)
    return status, song_id, credits


# ───────────────────────────────────────────────────────────────────────────
# RELATÓRIO DE VALIDAÇÃO
# ───────────────────────────────────────────────────────────────────────────


def print_validation_report(songs: list[Song], state: dict) -> tuple[list[Song], list[Song]]:
    """Imprime tabela e devolve (válidas, inválidas)."""
    table = Table(title=f"Validação — {songs[0].source_file if songs else ''}")
    table.add_column("#", justify="right")
    table.add_column("Título", overflow="fold", max_width=40)
    table.add_column("Letra", justify="right")
    table.add_column("Style", justify="right")
    table.add_column("Estado")
    table.add_column("Validação")

    valid: list[Song] = []
    invalid: list[Song] = []
    for s in songs:
        errs = validate_song(s)
        prev = state.get(s.key, {}).get("status")
        estado = prev if prev else "pendente"
        if errs:
            invalid.append(s)
            verdict = "[red]" + "; ".join(errs) + "[/red]"
        else:
            valid.append(s)
            verdict = "[green]ok[/green]"
        estado_fmt = f"[cyan]{estado}[/cyan]" if estado in DONE_STATUSES else estado
        table.add_row(
            str(s.index),
            s.title,
            str(len(s.lyrics)),
            str(len(s.style)),
            estado_fmt,
            verdict,
        )
    console.print(table)
    return valid, invalid


# ───────────────────────────────────────────────────────────────────────────
# SELEÇÃO DE ARQUIVO (um lote por execução)
# ───────────────────────────────────────────────────────────────────────────


def discover_files(dir_arg: Path) -> list[Path]:
    if dir_arg.is_file():
        return [dir_arg]
    return sorted(dir_arg.glob("*.md"))


def select_target_file(files: list[Path], state: dict) -> tuple[Optional[Path], list[Song]]:
    """Primeiro arquivo com ≥1 música ainda pendente."""
    for f in files:
        songs = parse_file(f)
        if any(state.get(s.key, {}).get("status") not in DONE_STATUSES for s in songs):
            return f, songs
    return None, []


# ───────────────────────────────────────────────────────────────────────────
# MAIN
# ───────────────────────────────────────────────────────────────────────────


def build_queue(songs: list[Song], state: dict, valid: list[Song], batch_size: int) -> list[Song]:
    valid_keys = {s.key for s in valid}
    pending = [
        s for s in songs
        if s.key in valid_keys and state.get(s.key, {}).get("status") not in DONE_STATUSES
    ]
    return pending[:batch_size]


def run(args: argparse.Namespace) -> int:
    dir_arg = Path(args.dir)
    if not dir_arg.exists():
        console.print(f"[red]--dir não existe: {dir_arg}[/red]")
        return 2

    state = load_state(STATE_FILE)
    files = discover_files(dir_arg)
    if not files:
        console.print(f"[red]Nenhum arquivo .md em {dir_arg}[/red]")
        return 2

    target, songs = select_target_file(files, state)
    if target is None:
        console.print("[green]Tudo concluído — nenhuma música pendente.[/green]")
        return 0

    console.rule(f"Lote alvo: {target.name}")
    valid, invalid = print_validation_report(songs, state)

    if invalid and not args.skip_invalid:
        console.print(
            f"[red]{len(invalid)} música(s) inválida(s). Abortando antes de abrir o "
            f"browser. Use --skip-invalid para ignorá-las.[/red]"
        )
        return 1

    queue = build_queue(songs, state, valid, args.batch_size)
    if not queue:
        console.print("[yellow]Sem músicas válidas pendentes neste lote.[/yellow]")
        return 0

    console.print(
        f"[bold]{len(queue)}[/bold] música(s) nesta execução "
        f"(batch-size={args.batch_size}, dry-run={args.dry_run})."
    )

    counts = {"completed": 0, "submitted_timeout": 0, "failed": 0, "dry_run": 0}
    consecutive_errors = 0
    last_credits: Optional[int] = None
    prev_credits: Optional[int] = None   # para detecção de freeze
    stop_reason: Optional[str] = None

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.connect_over_cdp(CDP_ENDPOINT)
        except Exception as e:
            console.print(
                f"[red]Falha ao conectar via CDP em {CDP_ENDPOINT}: {e}\n"
                f"Suba o Chrome com --remote-debugging-port=9222 e tente novamente.[/red]"
            )
            return 3

        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.pages[0] if context.pages else context.new_page()
        apply_stealth(page)

        progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            console=console,
        )
        try:
            with progress:
                task = progress.add_task("Processando", total=len(queue))
                for song in queue:
                    progress.update(task, description=f"#{song.index} {song.title[:30]}")
                    status, song_id, song_credits = "failed", None, None

                    # Até 2 tentativas: 1 normal + 1 fallback (só erros pré-submit).
                    for attempt in (1, 2):
                        try:
                            status, song_id, song_credits = process_song(page, song, args.dry_run)
                            break
                        except StopAll as e:
                            stop_reason = str(e)
                            log.error("stop_all", song=song.key, reason=stop_reason)
                            screenshot(page, f"stop_song{song.index}")
                            raise
                        except (PreSubmitError, PlaywrightError) as e:
                            log.warning(
                                "erro_pre_submit",
                                song=song.key,
                                attempt=attempt,
                                error=str(e),
                            )
                            screenshot(page, f"error_song{song.index}_try{attempt}")
                            if attempt == 1:
                                continue  # fallback uma vez
                            status, song_id = "failed", None

                    # Contabiliza + persiste
                    counts[status] = counts.get(status, 0) + 1

                    # Detecção de freeze no contador de créditos
                    if song_credits is not None and prev_credits is not None:
                        if song_credits == prev_credits:
                            log.warning(
                                "creditos_freeze",
                                song=song.key,
                                credits=song_credits,
                                msg="contador de créditos não atualizou — possível lag do Suno",
                            )
                    if song_credits is not None:
                        prev_credits = song_credits

                    if status == "dry_run":
                        # dry-run não altera o estado (real run ainda processará)
                        consecutive_errors = 0
                        progress.advance(task)
                        continue

                    entry = {"status": status, "title": song.title, "ts": _now()}
                    if song_id:
                        entry["song_id"] = song_id
                    if status == "failed":
                        entry["error"] = "ver screenshots/logs"
                    state[song.key] = entry
                    save_state(STATE_FILE, state)
                    log.info("estado_salvo", song=song.key, status=status, song_id=song_id)

                    if status == "failed":
                        consecutive_errors += 1
                        if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                            stop_reason = f"{MAX_CONSECUTIVE_ERRORS} erros consecutivos"
                            log.error("stop_all", reason=stop_reason)
                            progress.advance(task)
                            raise StopAll(stop_reason)
                    else:
                        consecutive_errors = 0

                    progress.advance(task)

                    # j. pausa entre músicas (execução real)
                    if not args.dry_run:
                        page.wait_for_timeout(BETWEEN_SONGS_MS)
        except StopAll:
            pass  # stop_reason já registrado; vai para o resumo
        except KeyboardInterrupt:
            stop_reason = "interrompido pelo usuário"
            console.print("\n[yellow]Interrompido — estado salvo.[/yellow]")

        # leitura final de créditos (best-effort)
        try:
            last_credits = read_credits(page)
        except Exception:
            last_credits = None

    # Resumo
    console.rule("Resumo")
    summary = Table(show_header=False)
    summary.add_row("Lote", target.name)
    summary.add_row("Concluídas", str(counts["completed"]))
    summary.add_row("Timeout (submetidas)", str(counts["submitted_timeout"]))
    summary.add_row("Falhas", str(counts["failed"]))
    summary.add_row("Dry-run", str(counts["dry_run"]))
    summary.add_row("Créditos restantes", str(last_credits if last_credits is not None else "?"))
    if stop_reason:
        summary.add_row("Parada", f"[red]{stop_reason}[/red]")
    console.print(summary)

    return 0


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Automação de criação de músicas no Suno AI.")
    p.add_argument("--dir", required=True, help="Diretório com os arquivos .md (ou um .md específico).")
    p.add_argument("--dry-run", action="store_true", help="Percorre tudo sem clicar Create.")
    p.add_argument("--batch-size", type=int, default=10, help="Máx. de músicas nesta execução (padrão: 10).")
    p.add_argument("--skip-invalid", action="store_true", help="Ignora arquivos com erro de validação.")
    return p.parse_args(argv)


def main() -> None:
    args = parse_args()
    try:
        sys.exit(run(args))
    except KeyboardInterrupt:
        console.print("\n[yellow]Abortado.[/yellow]")
        sys.exit(130)


if __name__ == "__main__":
    main()
