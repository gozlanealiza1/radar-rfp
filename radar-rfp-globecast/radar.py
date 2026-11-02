#!/usr/bin/env python3
"""
Radar RFP Globecast
-------------------
Va chercher les appels d'offres sur des sources publiques ouvertes,
garde ceux qui concernent le domaine Globecast (mots-clés, codes CPV,
comptes cibles), les centralise dans data/rfps.json et regénère la
page docs/index.html.

Lancé automatiquement chaque matin par GitHub Actions
(.github/workflows/radar.yml). Aucun réglage ici : tout est dans config.yaml.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import os
import re
import sys
import time
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent
DATA_FILE = ROOT / "data" / "rfps.json"
STATE_FILE = ROOT / "data" / "state.json"
TEMPLATE_FILE = ROOT / "site" / "template.html"
OUTPUT_FILE = ROOT / "docs" / "index.html"

TODAY = dt.date.today()
NOW_ISO = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "radar-rfp/1.0 (veille appels d'offres)"})
TIMEOUT = 60


# ---------------------------------------------------------------------
# Utilitaires
# ---------------------------------------------------------------------
def log(msg: str) -> None:
    print(msg, flush=True)


def norm(text: str) -> str:
    """minuscules, sans accents, 'canal+' -> 'canal plus'"""
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", str(text))
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = text.replace("canal+", "canal plus").replace("’", "'")
    return re.sub(r"\s+", " ", text)


def term_regex(term: str) -> re.Pattern:
    return re.compile(r"(?<![a-z0-9])" + re.escape(norm(term)) + r"(?![a-z0-9])")


def pick_text(value, prefs=("fra", "fre", "eng", "FRA", "FRE", "ENG")) -> str:
    """Extrait un texte lisible d'une valeur TED (str, liste, dict multilingue)."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        for v in value:
            t = pick_text(v, prefs)
            if t:
                return t
        return ""
    if isinstance(value, dict):
        for p in prefs:
            if p in value:
                t = pick_text(value[p], prefs)
                if t:
                    return t
        for v in value.values():
            t = pick_text(v, prefs)
            if t:
                return t
    return ""


def all_texts(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, int, float)):
        return [str(value)]
    if isinstance(value, list):
        out = []
        for v in value:
            out += all_texts(v)
        return out
    if isinstance(value, dict):
        out = []
        for v in value.values():
            out += all_texts(v)
        return out
    return []


def to_date(value) -> str:
    """Renvoie 'YYYY-MM-DD' ou '' à partir de formats variés."""
    text = pick_text(value) if not isinstance(value, str) else value
    if not text:
        return ""
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    if m:
        return m.group(0)
    m = re.search(r"(\d{2})/(\d{2})/(\d{4})", text)  # MM/DD/YYYY (SAM.gov)
    if m:
        return f"{m.group(3)}-{m.group(1)}-{m.group(2)}"
    m = re.search(r"\b(\d{4})(\d{2})(\d{2})\b", text)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return ""


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def since_date(state: dict, source: str, first_run_days: int) -> dt.date:
    last = state.get("sources", {}).get(source, {}).get("dernier_succes")
    if last:
        # 2 jours de recouvrement pour ne rien rater
        return dt.date.fromisoformat(last[:10]) - dt.timedelta(days=2)
    return TODAY - dt.timedelta(days=first_run_days)


# ---------------------------------------------------------------------
# Filtre "domaine Globecast"
# ---------------------------------------------------------------------
class Matcher:
    def __init__(self, cfg: dict):
        self.keywords = [(k, term_regex(k)) for k in cfg.get("mots_cles", [])]
        self.excluded = [(k, term_regex(k)) for k in cfg.get("mots_exclus", [])]
        self.cpv_prefixes = [str(p) for p in cfg.get("prefixes_cpv", [])]
        self.targets = [
            (t["nom"], [term_regex(n) for n in t.get("noms", [])] or [term_regex(t["nom"])])
            for t in cfg.get("comptes_cibles", [])
        ]

    def evaluate(self, title: str, description: str, buyer: str, cpvs: list[str]):
        text = norm(" ".join([title or "", description or ""]))
        reasons = [k for k, rx in self.keywords if rx.search(text)]
        cpv_hits = sorted({c for c in cpvs if any(str(c).startswith(p) for p in self.cpv_prefixes)})
        reasons += [f"CPV {c}" for c in cpv_hits]
        excluded = [k for k, rx in self.excluded if rx.search(text)]
        if excluded and not cpv_hits:
            reasons = []
        who = norm(" ".join([buyer or "", title or ""]))
        targets = [name for name, rxs in self.targets if any(rx.search(who) for rx in rxs)]
        return reasons, targets


def make_item(source, ref, title, buyer, country, published, deadline, url,
              notice_type, cpvs, reasons, targets, kind="rfp", description=""):
    return {
        "id": f"{source}:{ref}",
        "source": source,
        "ref": ref,
        "titre": (title or "").strip()[:400],
        "acheteur": (buyer or "").strip()[:200],
        "pays": country or "",
        "publie": published or "",
        "date_limite": deadline or "",
        "lien": url or "",
        "type": notice_type or "",
        "cpv": cpvs[:8],
        "pourquoi": reasons[:6],
        "comptes": targets,
        "nature": kind,
        "resume": (description or "").strip()[:300],
    }


# ---------------------------------------------------------------------
# Source 1 : TED (Journal officiel de l'UE), API publique sans clé
# ---------------------------------------------------------------------
TED_URL = "https://api.ted.europa.eu/v3/notices/search"
TED_FIELDS = [
    "publication-number", "notice-title", "buyer-name", "buyer-country",
    "publication-date", "deadline-receipt-tender-date-lot", "classification-cpv",
    "notice-type", "links",
]
TED_TYPES = {"cn": "Avis de marché", "can": "Attribution", "pin": "Préinformation",
             "veat": "Attribution", "pmc": "Avis de marché", "subco": "Avis de marché"}


def ted_query(q: str, max_pages: int = 10) -> list[dict]:
    notices = []
    for page in range(1, max_pages + 1):
        body = {"query": q, "fields": TED_FIELDS, "limit": 100, "page": page, "scope": "ALL"}
        r = HTTP.post(TED_URL, json=body, timeout=TIMEOUT)
        if r.status_code == 400:
            raise ValueError(f"TED a refusé la requête : {r.text[:300]}")
        r.raise_for_status()
        batch = r.json().get("notices", [])
        notices += batch
        if len(batch) < 100:
            break
        time.sleep(1)
    return notices


def ted_item(n: dict, matcher: Matcher):
    ref = pick_text(n.get("publication-number"))
    if not ref:
        return None
    title = pick_text(n.get("notice-title"))
    buyer = pick_text(n.get("buyer-name"))
    cpvs = [c for c in all_texts(n.get("classification-cpv")) if re.fullmatch(r"\d{8}", c)]
    reasons, targets = matcher.evaluate(title, "", buyer, cpvs)
    if not reasons and not targets:
        return None
    link = ""
    links = n.get("links") or {}
    if isinstance(links, dict):
        link = pick_text(links.get("html"), ("FRA", "fra", "ENG", "eng")) or pick_text(links)
    if not link.startswith("http"):
        link = f"https://ted.europa.eu/fr/notice/-/detail/{ref}"
    ntype_raw = pick_text(n.get("notice-type")).lower()
    ntype = next((v for k, v in TED_TYPES.items() if ntype_raw.startswith(k)), ntype_raw)
    deadlines = sorted(d for d in (to_date(x) for x in all_texts(n.get("deadline-receipt-tender-date-lot"))) if d)
    return make_item("TED", ref, title, buyer, pick_text(n.get("buyer-country")),
                     to_date(n.get("publication-date")), deadlines[-1] if deadlines else "",
                     link, ntype, cpvs, reasons, targets)


def fetch_ted(cfg, state, matcher):
    items, warnings = [], []
    first = cfg.get("premier_lancement_jours", {})
    since = since_date(state, "TED", first.get("ted", 60)).strftime("%Y%m%d")

    cpv_part = "classification-cpv IN (" + " ".join(cfg.get("codes_cpv", [])) + ")"
    kw_part = " OR ".join(f'FT="{k}"' for k in cfg.get("mots_cles", []))
    queries = [
        f"({cpv_part} OR {kw_part}) AND publication-date>={since}",
        f"{cpv_part} AND publication-date>={since}",  # repli si la 1re est refusée
    ]
    notices = None
    for q in queries:
        try:
            notices = ted_query(q)
            break
        except ValueError as e:
            warnings.append(str(e))
    if notices is None:
        raise RuntimeError(" | ".join(warnings))

    # Comptes cibles : tout ce qu'ils publient, sur une période plus longue
    since_t = since_date(state, "TED", first.get("ted_comptes_cibles", 365)).strftime("%Y%m%d")
    for t in cfg.get("comptes_cibles", []):
        for name in t.get("noms", [])[:2]:
            q = f'buyer-name~"{name}" AND publication-date>={since_t}'
            try:
                notices += ted_query(q, max_pages=3)
            except Exception as e:  # un compte qui échoue ne bloque pas les autres
                warnings.append(f"{t['nom']} : {e}")
            time.sleep(1)

    for n in notices:
        it = ted_item(n, matcher)
        if it:
            items.append(it)
    return items, warnings


# ---------------------------------------------------------------------
# Source 2 : BOAMP (France), open data DILA sans clé
# ---------------------------------------------------------------------
BOAMP_URL = "https://boamp-datadila.opendatasoft.com/api/explore/v2.1/catalog/datasets/boamp/records"
CPV_IN_TEXT = re.compile(r"\b(\d{8})\b")


def boamp_item(rec: dict, matcher: Matcher):
    ref = pick_text(rec.get("idweb")) or pick_text(rec.get("id"))
    if not ref:
        return None
    title = pick_text(rec.get("objet"))
    buyer = pick_text(rec.get("nomacheteur"))
    descr = " ".join(all_texts(rec.get("descripteur_libelle")))
    raw = rec.get("donnees")
    raw = raw if isinstance(raw, str) else json.dumps(raw or "", ensure_ascii=False)
    cpvs = sorted({c for c in CPV_IN_TEXT.findall(raw) if any(c.startswith(p) for p in matcher.cpv_prefixes)})
    reasons, targets = matcher.evaluate(title, descr, buyer, cpvs)
    if not reasons and not targets:
        return None
    link = pick_text(rec.get("url_avis"))
    if not link.startswith("http"):
        link = f"https://www.boamp.fr/pages/avis/?q=idweb:{ref}"
    return make_item("BOAMP", ref, title, buyer, "FRA", to_date(rec.get("dateparution")),
                     to_date(rec.get("datelimitereponse")), link,
                     pick_text(rec.get("nature_libelle")) or pick_text(rec.get("nature")),
                     cpvs, reasons, targets, description=descr)


def fetch_boamp(cfg, state, matcher):
    items = []
    start = since_date(state, "BOAMP", cfg.get("premier_lancement_jours", {}).get("boamp", 14))
    day = start
    while day <= TODAY:
        offset = 0
        while offset < 9900:
            params = {"where": f"dateparution=date'{day.isoformat()}'", "limit": 100,
                      "offset": offset, "order_by": "idweb"}
            r = HTTP.get(BOAMP_URL, params=params, timeout=TIMEOUT)
            r.raise_for_status()
            results = r.json().get("results", [])
            for rec in results:
                it = boamp_item(rec, matcher)
                if it:
                    items.append(it)
            if len(results) < 100:
                break
            offset += 100
        day += dt.timedelta(days=1)
    return items, []


# ---------------------------------------------------------------------
# Source 3 : Find a Tender (Royaume-Uni), API OCDS sans clé
# ---------------------------------------------------------------------
FTS_URL = "https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages"


def fts_item(rel: dict, matcher: Matcher):
    tender = rel.get("tender") or {}
    ref = rel.get("id") or rel.get("ocid")
    if not ref:
        return None
    title = tender.get("title") or rel.get("title") or ""
    descr = tender.get("description") or ""
    buyer = (rel.get("buyer") or {}).get("name", "")
    if not buyer:
        for p in rel.get("parties", []):
            if "buyer" in (p.get("roles") or []):
                buyer = p.get("name", "")
                break
    cpvs = []
    for c in [tender.get("classification") or {}] + (tender.get("additionalClassifications") or []):
        if str(c.get("scheme", "")).upper() == "CPV" and c.get("id"):
            cpvs.append(str(c["id"])[:8])
    for lot in tender.get("items") or []:
        for c in [lot.get("classification") or {}] + (lot.get("additionalClassifications") or []):
            if str(c.get("scheme", "")).upper() == "CPV" and c.get("id"):
                cpvs.append(str(c["id"])[:8])
    reasons, targets = matcher.evaluate(title, descr, buyer, cpvs)
    if not reasons and not targets:
        return None
    m = re.match(r"^(\d{6}-\d{4})", str(ref))
    link = (f"https://www.find-tender.service.gov.uk/Notice/{m.group(1)}" if m
            else "https://www.find-tender.service.gov.uk/Search/Results?keywords="
                 + urllib.parse.quote(str(rel.get("ocid") or ref)))
    tags = " ".join(rel.get("tag") or [])
    ntype = "Attribution" if "award" in tags else ("Préinformation" if "planning" in tags else "Avis de marché")
    return make_item("Find a Tender (UK)", str(ref), title, buyer, "GBR", to_date(rel.get("date")),
                     to_date((tender.get("tenderPeriod") or {}).get("endDate")), link, ntype,
                     sorted(set(cpvs)), reasons, targets, description=descr)


def fetch_fts(cfg, state, matcher):
    items = []
    since = since_date(state, "Find a Tender (UK)", cfg.get("premier_lancement_jours", {}).get("find_a_tender", 7))
    url = FTS_URL
    params = {"updatedFrom": since.strftime("%Y-%m-%dT00:00:00"), "limit": 100}
    for _ in range(200):
        r = HTTP.get(url, params=params, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        for rel in data.get("releases", []):
            it = fts_item(rel, matcher)
            if it:
                items.append(it)
        nxt = (data.get("links") or {}).get("next")
        if not nxt or not data.get("releases"):
            break
        url, params = nxt, None
        time.sleep(0.5)
    return items, []


# ---------------------------------------------------------------------
# Source 4 : SAM.gov (États-Unis) : clé gratuite requise (secret SAM_API_KEY)
# ---------------------------------------------------------------------
SAM_URL = "https://api.sam.gov/opportunities/v2/search"
SAM_TERMS = ["playout", "broadcast", "satellite uplink", "teleport", "video streaming", "IPTV"]


def fetch_sam(cfg, state, matcher):
    key = os.environ.get("SAM_API_KEY", "").strip()
    if not key:
        return None, ["Désactivé : ajoutez une clé SAM_API_KEY pour les États-Unis (voir le guide)."]
    items = []
    since = since_date(state, "SAM.gov (US)", cfg.get("premier_lancement_jours", {}).get("sam_gov", 30))
    for term in SAM_TERMS:
        params = {"api_key": key, "postedFrom": since.strftime("%m/%d/%Y"),
                  "postedTo": TODAY.strftime("%m/%d/%Y"), "title": term, "limit": 1000}
        r = HTTP.get(SAM_URL, params=params, timeout=TIMEOUT)
        r.raise_for_status()
        for o in r.json().get("opportunitiesData", []) or []:
            title = o.get("title", "")
            buyer = o.get("fullParentPathName") or o.get("department") or ""
            reasons, targets = matcher.evaluate(title, "", buyer, [])
            if not reasons:
                reasons = [term]
            items.append(make_item("SAM.gov (US)", o.get("noticeId", title), title, buyer, "USA",
                                   to_date(o.get("postedDate")), to_date(o.get("responseDeadLine")),
                                   o.get("uiLink", ""), o.get("type", ""), [], reasons, targets))
        time.sleep(1)
    return items, []


# ---------------------------------------------------------------------
# Source 5 : signaux presse (Google Actualités RSS)
# ---------------------------------------------------------------------
NEWS_URL = "https://news.google.com/rss/search"


def fetch_news(cfg, state, matcher):
    items = []
    for q in cfg.get("recherches_presse", []):
        for hl, gl, ceid in (("fr", "FR", "FR:fr"), ("en", "US", "US:en")):
            r = HTTP.get(NEWS_URL, params={"q": q + " when:30d", "hl": hl, "gl": gl, "ceid": ceid},
                         timeout=TIMEOUT)
            r.raise_for_status()
            root = ET.fromstring(r.content)
            for entry in root.iter("item"):
                title = html.unescape(entry.findtext("title") or "")
                link = entry.findtext("link") or ""
                src = entry.findtext("source") or ""
                pub = entry.findtext("pubDate") or ""
                try:
                    published = dt.datetime.strptime(pub[:25].strip(), "%a, %d %b %Y %H:%M:%S").date().isoformat()
                except ValueError:
                    published = ""
                reasons, targets = matcher.evaluate(title, "", "", [])
                items.append(make_item("Presse", link, title, src, "", published, "", link,
                                       "Article", [], reasons or [f"recherche : {q}"], targets, kind="signal"))
            time.sleep(1)
    return items, []


# ---------------------------------------------------------------------
# Assemblage
# ---------------------------------------------------------------------
SOURCES = [
    ("ted", "TED", "Europe : Journal officiel de l'UE", fetch_ted),
    ("boamp", "BOAMP", "France : marchés publics", fetch_boamp),
    ("find_a_tender", "Find a Tender (UK)", "Royaume-Uni", fetch_fts),
    ("sam_gov", "SAM.gov (US)", "États-Unis : marchés fédéraux", fetch_sam),
    ("presse", "Presse", "Google Actualités : acteurs privés", fetch_news),
]


def merge(existing: list[dict], new: list[dict], keep_days: int) -> list[dict]:
    first_run = not existing
    by_id = {it["id"]: it for it in existing}
    for it in new:
        old = by_id.get(it["id"])
        if old:
            it["vu_le"] = old.get("vu_le", TODAY.isoformat())
            it["comptes"] = sorted(set(old.get("comptes", [])) | set(it["comptes"]))
            it["pourquoi"] = list(dict.fromkeys(old.get("pourquoi", []) + it["pourquoi"]))[:6]
        elif first_run and it.get("publie"):
            # 1er passage : l'historique récupéré n'est pas marqué "Nouveau"
            it["vu_le"] = it["publie"]
        else:
            it["vu_le"] = TODAY.isoformat()
        by_id[it["id"]] = it
    limit = (TODAY - dt.timedelta(days=keep_days)).isoformat()
    kept = [it for it in by_id.values() if (it.get("publie") or it.get("vu_le", "")) >= limit]
    kept.sort(key=lambda x: (x.get("publie") or x.get("vu_le") or ""), reverse=True)
    return kept


def build_page(cfg: dict, items: list[dict], state: dict) -> None:
    template = TEMPLATE_FILE.read_text(encoding="utf-8")
    payload = {
        "titre": cfg.get("titre", "Radar RFP"),
        "maj": NOW_ISO,
        "aujourdhui": TODAY.isoformat(),
        "comptes": [t["nom"] for t in cfg.get("comptes_cibles", [])],
        "sources": state.get("sources", {}),
        "items": items,
    }
    blob = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    page = template.replace("/*__RADAR_DATA__*/null", blob).replace("__TITRE__", html.escape(payload["titre"]))
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(page, encoding="utf-8")
    (OUTPUT_FILE.parent / ".nojekyll").write_text("", encoding="utf-8")


def main() -> int:
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    state = load_json(STATE_FILE, {"sources": {}})
    state.setdefault("sources", {})
    existing = load_json(DATA_FILE, [])
    matcher = Matcher(cfg)
    enabled = cfg.get("sources", {})

    collected = []
    for key, label, description, fn in SOURCES:
        st = state["sources"].setdefault(label, {})
        st["description"] = description
        if not enabled.get(key, True):
            st.update({"etat": "désactivée", "message": "Désactivée dans config.yaml", "dernier_essai": NOW_ISO})
            continue
        log(f"→ {label}…")
        try:
            items, warnings = fn(cfg, state, matcher)
            if items is None:
                st.update({"etat": "désactivée", "message": " ".join(warnings), "dernier_essai": NOW_ISO})
                log(f"   désactivée : {' '.join(warnings)}")
                continue
            collected += items
            st.update({"etat": "ok", "dernier_essai": NOW_ISO, "dernier_succes": NOW_ISO,
                       "trouves_dernier_passage": len(items),
                       "message": ("Avertissement : " + " | ".join(warnings)[:400]) if warnings else ""})
            log(f"   {len(items)} résultat(s) retenu(s)")
        except Exception as e:
            st.update({"etat": "erreur", "dernier_essai": NOW_ISO, "message": f"{type(e).__name__} : {str(e)[:400]}"})
            log(f"   ERREUR : {e}")

    items = merge(existing, collected, int(cfg.get("conservation_jours", 400)))
    state["dernier_passage"] = NOW_ISO
    save_json(DATA_FILE, items)
    save_json(STATE_FILE, state)
    build_page(cfg, items, state)
    log(f"Page générée : {len(items)} avis au total → {OUTPUT_FILE.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
