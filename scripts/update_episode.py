#!/usr/bin/env python3
"""
Prüft den freeze & talk YouTube-Feed auf eine neue Folge und aktualisiert
index.html (Hero, Folgen-Liste, Zitat). Läuft im GitHub Action – rechner-
unabhängig. Deterministische HTML-Edits in Python; das Zitat wird (optional)
von Claude aus dem Transkript gewählt, wenn ANTHROPIC_API_KEY gesetzt ist.

Env:
  ANTHROPIC_API_KEY   optional – aktiviert die automatische Zitat-Auswahl
  SIMULATE_CURRENT_ID optional – überschreibt die "aktuelle" Hero-ID (nur Test)
  DRY_RUN=1           optional – schreibt index.html nicht, nur Log/Diff-Info

Exit-Code 0 = ok (egal ob geändert oder nicht). Nicht-0 nur bei echtem Fehler.
Schreibt "changed=true|false" nach $GITHUB_OUTPUT (falls gesetzt).
"""
import os, re, sys, json, html, urllib.request, urllib.error

CHANNEL = "UCOYIpV_B4ECS51xwBbnH1tQ"
RSS = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL}"
INDEX = os.path.join(os.path.dirname(__file__), "..", "index.html")
MONTHS = ["Januar","Februar","März","April","Mai","Juni","Juli","August",
          "September","Oktober","November","Dezember"]

def log(*a): print(*a, flush=True)

def gh_output(**kv):
    p = os.environ.get("GITHUB_OUTPUT")
    if p:
        with open(p, "a") as f:
            for k, v in kv.items():
                f.write(f"{k}={v}\n")

def set_output(changed):
    gh_output(changed="true" if changed else "false")

def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

def fetch(url, timeout=30):
    last = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA,
                "Accept": "application/atom+xml,text/xml,*/*"})
            return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace")
        except Exception as e:
            last = e
            import time; time.sleep(2 * (attempt + 1))
    raise last

def feed_text():
    ff = os.environ.get("FEED_FILE")
    if ff:
        return open(ff, encoding="utf-8").read()
    return fetch(RSS)

def newest_episode():
    data = feed_text()
    for block in re.findall(r"<entry>(.*?)</entry>", data, re.S):
        vid = re.search(r"<yt:videoId>([\w-]+)</yt:videoId>", block)
        tm = re.search(r"<title>(.*?)</title>", block, re.S)
        pm = re.search(r"<published>(.*?)</published>", block, re.S)
        if not (vid and tm):
            continue
        title = html.unescape(tm.group(1)).strip()
        if re.search(r"freeze\s*&\s*talk\s*#\s*\d", title, re.I):
            return {"id": vid.group(1), "title": title,
                    "published": pm.group(1).strip() if pm else ""}
    return None

def parse_title(title):
    """'<Titel> – <Gast> | <Firma> | freeze & talk #N' -> dict"""
    num = None
    m = re.search(r"#\s*(\d+)", title)
    if m: num = m.group(1)
    # Nummer/Serien-Suffix abschneiden
    core = re.split(r"\|\s*freeze\s*&\s*talk", title, flags=re.I)[0].strip()
    parts = re.split(r"\s+[–—-]\s+", core, maxsplit=1)
    short = parts[0].strip()
    guest, company = "", ""
    if len(parts) > 1:
        segs = [s.strip() for s in parts[1].split("|") if s.strip()]
        if segs:
            guest = segs[0]
            if len(segs) > 1:
                company = " · ".join(segs[1:])
    return {"num": num or "?", "title": short, "guest": guest, "company": company}

def show_line(guest, company):
    is_person = bool(re.match(r"^[A-ZÄÖÜ][\wäöüß.-]+\s+[A-ZÄÖÜ][\wäöüß.-]+", guest)) and "geschichte" not in guest.lower()
    left = f"mit {guest}" if (guest and is_person) else guest
    parts = [p for p in [left, company] if p]
    return " · ".join(parts) if parts else "freeze & talk"

def date_de(published):
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", published or "")
    if not m: return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    return f"{d}. {MONTHS[mo-1]} {y}"

def get_transcript(vid):
    try:
        from youtube_transcript_api import YouTubeTranscriptApi as Y
        try:
            data = Y.get_transcript(vid, languages=["de", "de-DE"])
        except Exception:
            data = [{"text": s.text} for s in Y().fetch(vid, languages=["de", "de-DE"])]
        return " ".join(d["text"].replace("\n", " ") for d in data)
    except Exception as e:
        log("Transkript nicht verfügbar:", e)
        return None

def _prompt(transcript, ep):
    return (
        "Du bist Redakteur für den deutschen Gesundheits-Podcast 'freeze & talk'. "
        "Wähle aus dem folgenden Transkript EIN einzelnes, starkes, in sich verständliches Zitat "
        f"des Gastes ({ep['guest']}), das die Folge '{ep['title']}' gut repräsentiert. "
        "Regeln: nur echte Wörter aus dem Transkript, nichts erfinden; ein vollständiger Satz, "
        "prägnant (ca. 60–160 Zeichen); Füllwörter (ähm, halt, sozusagen) entfernen und Groß-/"
        "Kleinschreibung und Satzzeichen korrigieren, ohne die Aussage zu verändern. "
        "Antworte AUSSCHLIESSLICH mit JSON: {\"quote\":\"...\"}.\n\nTRANSKRIPT:\n" + transcript[:14000]
    )

def _extract_json_quote(text):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    q = json.loads(m.group(0)).get("quote", "").strip().strip('"„“”')
    return q or None

def _anthropic_quote(transcript, ep):
    key = os.environ["ANTHROPIC_API_KEY"]
    body = json.dumps({"model": "claude-haiku-4-5-20251001", "max_tokens": 300,
                       "messages": [{"role": "user", "content": _prompt(transcript, ep)}]}).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body,
        headers={"content-type": "application/json", "x-api-key": key, "anthropic-version": "2023-06-01"})
    try:
        resp = json.loads(urllib.request.urlopen(req, timeout=60).read())
        return _extract_json_quote("".join(b.get("text", "") for b in resp.get("content", [])))
    except Exception as e:
        log("Anthropic-Zitat Fehler:", e); return None

def _gemini_quote(transcript, ep):
    key = os.environ["GEMINI_API_KEY"]
    model = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    body = json.dumps({"contents": [{"parts": [{"text": _prompt(transcript, ep)}]}]}).encode()
    req = urllib.request.Request(url, data=body, headers={"content-type": "application/json"})
    try:
        resp = json.loads(urllib.request.urlopen(req, timeout=60).read())
        text = resp["candidates"][0]["content"]["parts"][0]["text"]
        return _extract_json_quote(text)
    except Exception as e:
        log("Gemini-Zitat Fehler:", e); return None

FILLERS = re.compile(r"\b([Ää]hm?|halt|sozusagen|quasi|gell|ne)\b[,]?\s*", re.I)

def heuristic_quote(transcript):
    """Kostenlose Auswahl ganz ohne KI: bester klarer Aussagesatz aus dem Transkript."""
    t = re.sub(r"\[[^\]]*\]", " ", transcript)       # [gelächter] u.ä. entfernen
    t = re.sub(r"\s+", " ", t)
    KW = ["training", "gesünd", "gesund", "körper", "ernährung", "regeneration", "stress",
          "schlaf", "bewegung", "kraft", "mental", "alltag", "respekt", "ziel", "muskel",
          "verletzung", "balance", "energie", "erholung"]
    best, best_score = None, 2   # Mindest-Score – sonst lieber kein Zitat
    for s in re.split(r"(?<=[.!?])\s+", t):
        s = s.strip()
        if not (60 <= len(s) <= 165) or s.endswith("?"):
            continue
        low = s.lower()
        if any(g in low for g in ["freent", "frezen", "blabla", "carlos", "insta"]):
            continue
        if re.match(r"^(Also|Ja|Aber|Genau|Und|Weil|Ähm|Ne|So )\b", s):
            continue
        score = sum(2 for kw in KW if kw in low)
        if re.search(r"\b(ist|kann|sollte|muss|bedeutet|geht darum)\b", low):
            score += 1
        if score > best_score:
            best, best_score = s, score
    if not best:
        return None
    best = FILLERS.sub("", best)
    best = re.sub(r"\s+([,.!?])", r"\1", re.sub(r"\s+", " ", best)).strip()
    return best[0].upper() + best[1:]

def pick_quote(transcript, ep):
    if not transcript:
        return None
    if os.environ.get("ANTHROPIC_API_KEY"):
        q = _anthropic_quote(transcript, ep)
        if q:
            return q
    if os.environ.get("GEMINI_API_KEY"):
        q = _gemini_quote(transcript, ep)
        if q:
            return q
    return heuristic_quote(transcript)  # gratis, ohne Key

def sub1(pattern, repl, text, flags=0):
    new, n = re.subn(pattern, repl, text, count=1, flags=flags)
    if n != 1:
        raise RuntimeError(f"Pattern nicht gefunden/eindeutig: {pattern[:60]}")
    return new

def set_quote(doc, quote, guest, num):
    doc = sub1(r'(<blockquote class="quote[^>]*>[\s\S]*?<p>)[\s\S]*?(</p>)',
               lambda mm: mm.group(1) + esc(quote) + mm.group(2), doc)
    attrib = esc((f"{guest} · " if guest else "") + f"Folge #{num}")
    doc = sub1(r'(<blockquote class="quote[^>]*>[\s\S]*?<cite>)[\s\S]*?(</cite>)',
               lambda mm: mm.group(1) + attrib + mm.group(2), doc)
    return doc

def finalize(doc, nid, msg):
    if not (doc.strip().startswith("<!DOCTYPE") and nid in doc):
        raise RuntimeError("Ergebnis sieht ungültig aus – abgebrochen.")
    if os.environ.get("DRY_RUN") == "1":
        open(INDEX + ".preview", "w", encoding="utf-8").write(doc)
        log("[DRY_RUN]", msg); gh_output(changed="true"); return
    open(INDEX, "w", encoding="utf-8").write(doc)
    log(msg); gh_output(changed="true", commit_msg=msg)

def main():
    ep = newest_episode()
    if not ep:
        log("Keine freeze & talk Folge im Feed gefunden."); set_output(False); return
    p = parse_title(ep["title"])
    log("Neueste Folge im Feed:", f"#{p['num']}", p["title"], f"({ep['id']})")

    doc = open(INDEX, encoding="utf-8").read()
    m = re.search(r'<a class="video" href="https://www\.youtube\.com/watch\?v=([\w-]+)"', doc)
    current_id = os.environ.get("SIMULATE_CURRENT_ID") or (m.group(1) if m else None)
    log("Aktuelle Hero-ID:", current_id)

    if current_id == ep["id"]:
        # Keine neue Folge – aber evtl. Zitat nachziehen, falls es beim ersten
        # Lauf noch kein Transkript gab und es jetzt verfügbar ist.
        cm = re.search(r'<cite>[\s\S]*?Folge\s*#?(\d+)\s*</cite>', doc)
        cite_num = cm.group(1) if cm else None
        if cite_num != p["num"]:
            q = pick_quote(get_transcript(current_id), p)
            if q:
                doc = set_quote(doc, q, p["guest"], p["num"])
                finalize(doc, current_id, f"Zitat nachgezogen für Folge #{p['num']}")
                return
            log(f"Keine neue Folge; Zitat für #{p['num']} noch nicht verfügbar (Transkript fehlt).")
            set_output(False); return
        log(f"Keine neue Folge – aktuell ist weiterhin #{p['num']}, Zitat aktuell.")
        set_output(False); return

    # --- alte Hero-Daten sichern (für die Folgen-Liste) ---
    old_id = re.search(r'<a class="video" href="https://www\.youtube\.com/watch\?v=([\w-]+)"', doc).group(1)
    old_show = re.search(r'<div class="show">(.*?)</div>', doc, re.S).group(1).strip()
    old_title = re.search(r'<div class="title">(.*?)</div>', doc, re.S).group(1).strip()
    old_num = re.search(r'<span class="ep">#?(\d+)</span>', doc).group(1)

    date = date_de(ep["published"]) or ""
    show = esc(show_line(p["guest"], p["company"]))
    title_e = esc(p["title"]); nid = ep["id"]; num = p["num"]

    # --- Hero aktualisieren ---
    doc = sub1(r'(<a class="video" href="https://www\.youtube\.com/watch\?v=)[\w-]+(")',
               lambda mm: mm.group(1) + nid + mm.group(2), doc)
    doc = sub1(r'(<a class="video"[^>]*aria-label=")[^"]*(")',
               lambda mm: mm.group(1) + f"freeze &amp; talk #{num} auf YouTube ansehen" + mm.group(2), doc)
    doc = sub1(r'(<img class="thumb" alt=")[^"]*(")',
               lambda mm: mm.group(1) + f"freeze &amp; talk #{num}" + mm.group(2), doc)
    doc = sub1(r'(<img class="thumb"[\s\S]*?src="https://i\.ytimg\.com/vi/)[\w-]+(/maxresdefault\.jpg")',
               lambda mm: mm.group(1) + nid + mm.group(2), doc)
    doc = sub1(r"(this\.src='https://i\.ytimg\.com/vi/)[\w-]+(/hqdefault\.jpg')",
               lambda mm: mm.group(1) + nid + mm.group(2), doc)
    if date:
        doc = sub1(r'(<span class="hh-date">)[^<]*(</span>)',
                   lambda mm: mm.group(1) + esc(date) + mm.group(2), doc)
    doc = sub1(r'(<div class="show">)[\s\S]*?(</div>)',
               lambda mm: mm.group(1) + show + mm.group(2), doc)
    doc = sub1(r'(<div class="title">)[\s\S]*?(</div>)',
               lambda mm: mm.group(1) + title_e + mm.group(2), doc)
    doc = sub1(r'(<span class="ep">)#?\d+(</span>)',
               lambda mm: mm.group(1) + f"#{num}" + mm.group(2), doc)

    # --- Folgen-Liste: alte Hero-Folge oben einfügen, älteste entfernen ---
    cards = re.findall(r'<a class="epcard[\s\S]*?</a>', doc)
    if cards:
        newcard = (
            '<a class="epcard frost rise d4" href="https://www.youtube.com/watch?v=' + old_id + '" '
            'target="_blank" rel="noopener">\n'
            '      <span class="th">\n'
            '        <img src="https://i.ytimg.com/vi/' + old_id + '/mqdefault.jpg" alt="" loading="lazy" />\n'
            '        <span class="pl"><svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg></span>\n'
            '      </span>\n'
            '      <span class="ei">\n'
            '        <span class="en">Folge #' + old_num + '</span>\n'
            '        <span class="et">' + old_title + '</span>\n'
            '        <span class="eg">' + old_show + '</span>\n'
            '      </span>\n'
            '    </a>'
        )
        keep = cards[:-1]  # älteste (letzte) rausfallen lassen
        new_block = ("\n\n    ").join([newcard] + keep)
        start = doc.find(cards[0]); end = doc.find(cards[-1]) + len(cards[-1])
        doc = doc[:start] + new_block + doc[end:]

    # --- Zitat ---
    quote = pick_quote(get_transcript(nid), p)
    if quote:
        doc = set_quote(doc, quote, p["guest"], num)
        log(f"Zitat gesetzt: „{quote}“")
    else:
        log("Zitat noch nicht gesetzt (Transkript fehlt) – wird an einem der nächsten Läufe automatisch nachgezogen.")

    finalize(doc, nid, f"Neue Folge #{num}: {p['title']}")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log("FEHLER:", e); sys.exit(1)
