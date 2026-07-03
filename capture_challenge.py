#!/usr/bin/env python3
"""
capture_challenge.py — Observador SÓ-LEITURA do desafio anti-robô do Suno.

Conecta a um Chrome já aberto (mesmo endpoint CDP do suno_agent) como um SEGUNDO
cliente e observa a página em intervalos, SEM clicar/navegar/digitar. Quando algo
parecido com captcha aparece (iframe de terceiros visível, modal, ou texto de
desafio em qualquer frame), salva um artefato completo — logs/challenge_capture_*.json
+ screenshot — e (opcional) emite um alerta sonoro. Serve para CAPTURAR exatamente o
que o Suno renderiza, já que o desafio é transitório.

Uso:
    python capture_challenge.py                 # observa, salva só quando detecta
    python capture_challenge.py --verbose       # salva um snapshot a cada poll
    python capture_challenge.py --interval 3    # intervalo do poll (s), padrão 4
    python capture_challenge.py --no-buzz       # não tocar alerta ao detectar
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    import winsound
except Exception:
    winsound = None

from playwright.sync_api import sync_playwright, Error as PlaywrightError

CDP_ENDPOINT = "http://localhost:9222"
LOGS_DIR = Path("logs")
SHOTS_DIR = Path("screenshots")

# Hosts "normais" da página do Suno que NÃO são captcha (evita falso alarme).
BENIGN_IFRAME_HOSTS = (
    "suno.com", "stripe.com", "onetrust", "google-analytics", "googletagmanager",
    "doubleclick", "youtube.com", "vimeo.com", "segment", "sentry", "intercom",
)
# Tokens de captcha em src/title de iframe (amplos — é captura, não produção).
CAPTCHA_IFRAME_TOKENS = (
    "hcaptcha", "recaptcha", "captcha", "bframe", "challenge", "turnstile",
    "challenges.cloudflare.com", "arkose", "funcaptcha", "perimeterx", "px-captcha",
    "geo.captcha", "datadome",
)
# Texto de desafio (PT+EN) — inclui frases típicas do hCaptcha por imagem.
CAPTCHA_TEXT_TOKENS = (
    "encontre todas", "selecione todas", "clique em cada", "clique em todas",
    "please click", "select all", "verify you are human", "are you a robot",
    "i'm not a robot", "not a robot", "press and hold", "checking your browser",
    "unusual traffic", "security check", "verifique que você", "confirme que você",
    "verificação de segurança", "prove que você",
)

# Inventário completo do frame principal (iframes + modais + texto).
JS_INVENTORY = """
() => {
  const vis = el => {
    const s = getComputedStyle(el), r = el.getBoundingClientRect();
    return s.display !== 'none' && s.visibility !== 'hidden'
        && parseFloat(s.opacity || '1') > 0.05 && r.width > 8 && r.height > 8;
  };
  const iframes = [...document.querySelectorAll('iframe')].map(f => {
    const r = f.getBoundingClientRect();
    return { src: f.getAttribute('src'), title: f.getAttribute('title'),
             id: f.id, cls: (f.className || '').toString().slice(0, 80),
             w: Math.round(r.width), h: Math.round(r.height), vis: vis(f) };
  });
  const modals = [...document.querySelectorAll('[role="dialog"],[aria-modal="true"],.modal')]
    .filter(vis).map(m => ({ tag: m.tagName, role: m.getAttribute('role'),
             cls: (m.className || '').toString().slice(0, 80),
             txt: (m.innerText || '').slice(0, 200) }));
  return { iframes, modals, bodyText: (document.body ? document.body.innerText.slice(0, 400) : '') };
}
"""


def _host_of(src: str) -> str:
    if not src:
        return ""
    s = src.split("://", 1)[-1]
    return s.split("/", 1)[0].lower()


def snapshot(page) -> dict:
    """Coleta inventário do frame principal + URL/texto de todos os frames."""
    info: dict = {}
    try:
        info["url"] = page.url
    except PlaywrightError as e:
        info["url_err"] = str(e)[:80]
    try:
        info.update(page.evaluate(JS_INVENTORY))
    except PlaywrightError as e:
        info["inv_err"] = str(e)[:120]
    frames = []
    try:
        for fr in page.frames:
            fi = {"url": (fr.url or "")[:160]}
            try:
                fi["txt"] = (fr.evaluate("() => document.body ? document.body.innerText.slice(0,200) : ''") or "").replace("\n", " ")
            except PlaywrightError as e:
                fi["txt_err"] = str(e)[:50]
            frames.append(fi)
    except PlaywrightError as e:
        info["frames_err"] = str(e)[:80]
    info["frames"] = frames
    return info


def looks_like_captcha(info: dict) -> tuple[bool, str]:
    """Heurística ampla: iframe de captcha/terceiros visível, modal, ou texto de desafio."""
    for f in info.get("iframes", []):
        src = (f.get("src") or "").lower()
        title = (f.get("title") or "").lower()
        if any(tok in src or tok in title for tok in CAPTCHA_IFRAME_TOKENS):
            return True, f"iframe-token:{_host_of(src) or title}"
        if f.get("vis") and src:
            host = _host_of(src)
            if host and not any(b in host for b in BENIGN_IFRAME_HOSTS):
                return True, f"iframe-3p:{host}"
    if info.get("modals"):
        return True, "modal"
    blobs = [info.get("bodyText", "")] + [fr.get("txt", "") for fr in info.get("frames", [])]
    for txt in blobs:
        low = (txt or "").lower()
        for tok in CAPTCHA_TEXT_TOKENS:
            if tok in low:
                return True, f"text:{tok}"
    for fr in info.get("frames", []):
        if any(tok in (fr.get("url") or "").lower() for tok in CAPTCHA_IFRAME_TOKENS):
            return True, f"frame-url:{fr.get('url')[:60]}"
    return False, ""


def buzz():
    if winsound is None:
        try:
            sys.stdout.write("\a"); sys.stdout.flush()
        except Exception:
            pass
        return
    try:
        for freq in (1000, 1400):
            winsound.Beep(freq, 300)
    except Exception:
        try:
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        except Exception:
            pass


def save(info_all: list, tag: str, page0) -> str:
    LOGS_DIR.mkdir(exist_ok=True)
    SHOTS_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    jpath = LOGS_DIR / f"challenge_capture_{tag}_{ts}.json"
    jpath.write_text(json.dumps(info_all, ensure_ascii=False, indent=2), encoding="utf-8")
    if page0 is not None:
        try:
            page0.screenshot(path=str(SHOTS_DIR / f"challenge_{tag}_{ts}.png"))
        except PlaywrightError:
            pass
    return str(jpath)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=4.0)
    ap.add_argument("--verbose", action="store_true", help="salva um snapshot a cada poll")
    ap.add_argument("--no-buzz", action="store_true", help="não tocar alerta ao detectar")
    args = ap.parse_args()

    print(f"[capture] observador SÓ-LEITURA em {CDP_ENDPOINT} | intervalo={args.interval}s | "
          f"verbose={args.verbose} | buzz={not args.no_buzz}", flush=True)
    polls, hits = 0, 0
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.connect_over_cdp(CDP_ENDPOINT)
        except Exception as e:
            print(f"[capture] falha ao conectar via CDP: {e}", flush=True)
            return 1
        while True:
            polls += 1
            all_pages = []
            page0 = None
            try:
                for ctx in browser.contexts:
                    for page in ctx.pages:
                        if page0 is None:
                            page0 = page
                        all_pages.append((page, snapshot(page)))
            except PlaywrightError as e:
                print(f"[capture] erro no poll: {str(e)[:80]}", flush=True)
                time.sleep(args.interval)
                continue

            detected, reason = False, ""
            for _pg, info in all_pages:
                d, r = looks_like_captcha(info)
                if d:
                    detected, reason = True, r
                    break

            dump = [info for _pg, info in all_pages]
            if detected:
                hits += 1
                path = save(dump, "HIT", page0)
                print(f"[capture] 🔴 DESAFIO DETECTADO ({reason}) — salvo {path}", flush=True)
                if not args.no_buzz:
                    buzz()
            elif args.verbose:
                save(dump, "poll", page0)

            if polls % 15 == 0:
                print(f"[capture] vivo — {polls} polls, {hits} detecções", flush=True)
            time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
