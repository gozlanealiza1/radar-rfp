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
    """Score de pertinence : mot fort = 3 pts, mot faible = 1 pt, CPV broadcast = 1 à 3 pts.
    Un avis est retenu s'il atteint le seuil (ou s'il vient d'un compte cible avec >= 1 pt)."""

    STRONG, WEAK = 3, 1

    def __init__(self, cfg: dict):
        strong = cfg.get("mots_cles_forts") or cfg.get("mots_cles") or []
        weak = cfg.get("mots_cles_faibles") or []
        self.strong = [(k, term_regex(k)) for k in strong if str(k).strip()]
        self.weak = [(k, term_regex(k)) for k in weak if str(k).strip()]
        self.all_terms = [k for k, _ in self.strong + self.weak]
        self.excluded = [(k, term_regex(k)) for k in cfg.get("mots_exclus", []) if str(k).strip()]
        self.threshold = int(cfg.get("seuil", 3))
        weights = cfg.get("poids_cpv") or {str(p): 3 for p in cfg.get("prefixes_cpv", [])}
        self.cpv_weights = sorted(((str(k), int(v)) for k, v in weights.items()), key=lambda kv: -len(kv[0]))
        self.cpv_prefixes = [p for p, _ in self.cpv_weights]
        self.targets_all = bool(cfg.get("comptes_cibles_tout", False))
        self.targets = [
            (t["nom"], [term_regex(n) for n in t.get("noms", [])] or [term_regex(t["nom"])])
            for t in cfg.get("comptes_cibles", [])
        ]
        self.tender_words = [term_regex(w) for w in cfg.get("presse_mots_obligatoires", [])]

    def cpv_weight(self, code: str) -> int:
        for prefix, w in self.cpv_weights:
            if str(code).startswith(prefix):
                return w
        return 0

    def evaluate(self, title: str, description: str, buyer: str, cpvs: list[str]):
        """Renvoie (raisons, comptes_cibles, score, retenu)."""
        text = norm(" ".join([title or "", description or ""]))
        strong_hits = [k for k, rx in self.strong if rx.search(text)]
        weak_hits = [k for k, rx in self.weak if rx.search(text)]
        # un mot inclus dans un autre déjà trouvé ne compte pas deux fois
        # ("streaming" dans "live streaming", "satellite" dans "liaison satellite")
        found = [norm(k) for k in strong_hits + weak_hits]
        def inside_other(k):
            nk = norm(k)
            return any(nk != o and term_regex(nk).search(o) for o in found)
        strong_hits = [k for k in strong_hits if not inside_other(k)]
        weak_hits = [k for k in weak_hits if not inside_other(k)]
        cpv_scored = sorted(((self.cpv_weight(c), c) for c in set(cpvs)), reverse=True)
        cpv_scored = [(w, c) for w, c in cpv_scored if w > 0]
        best_cpv = cpv_scored[0][0] if cpv_scored else 0
        score = self.STRONG * len(strong_hits) + self.WEAK * len(weak_hits) + best_cpv
        reasons = strong_hits + weak_hits + [f"CPV {c}" for _, c in cpv_scored[:2]]
        if any(rx.search(text) for _, rx in self.excluded):
            score, reasons = 0, []  # mot exclu : l'avis est écarté
        who = norm(" ".join([buyer or "", title or ""]))
        targets = [name for name, rxs in self.targets if any(rx.search(who) for rx in rxs)]
        keep = score >= self.threshold or bool(targets and (self.targets_all or score >= 1))
        return reasons, targets, score, keep

    def is_tender_title(self, title: str) -> bool:
        t = norm(title)
        return not self.tender_words or any(rx.search(t) for rx in self.tender_words)


def make_item(source, ref, title, buyer, country, published, deadline, url,
              notice_type, cpvs, reasons, targets, kind="rfp", description="", score=0):
    return {
        "score": score,
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


def ted_query(q: str, max_pages: int = 20) -> list[dict]:
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
    reasons, targets, score, keep = matcher.evaluate(title, "", buyer, cpvs)
    if not keep:
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
                     link, ntype, cpvs, reasons, targets, score=score)


def fetch_ted(cfg, state, matcher):
    items, warnings = [], []
    first = cfg.get("premier_lancement_jours", {})
    since = since_date(state, "TED", first.get("ted", 60)).strftime("%Y%m%d")

    cpv_part = "classification-cpv IN (" + " ".join(cfg.get("codes_cpv", [])) + ")"
    # Seuls les mots FORTS sont envoyés à TED (les faibles ramèneraient des milliers d'avis) ;
    # le tri fin est ensuite fait par le score.
    kw_part = " OR ".join(f'FT="{k}"' for k, _ in matcher.strong if '"' not in k)
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
    reasons, targets, score, keep = matcher.evaluate(title, descr, buyer, cpvs)
    if not keep:
        return None
    link = pick_text(rec.get("url_avis"))
    if not link.startswith("http"):
        link = f"https://www.boamp.fr/pages/avis/?q=idweb:{ref}"
    return make_item("BOAMP", ref, title, buyer, "FRA", to_date(rec.get("dateparution")),
                     to_date(rec.get("datelimitereponse")), link,
                     pick_text(rec.get("nature_libelle")) or pick_text(rec.get("nature")),
                     cpvs, reasons, targets, description=descr, score=score)


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
    reasons, targets, score, keep = matcher.evaluate(title, descr, buyer, cpvs)
    if not keep:
        return None
    m = re.match(r"^(\d{6}-\d{4})", str(ref))
    link = (f"https://www.find-tender.service.gov.uk/Notice/{m.group(1)}" if m
            else "https://www.find-tender.service.gov.uk/Search/Results?keywords="
                 + urllib.parse.quote(str(rel.get("ocid") or ref)))
    tags = " ".join(rel.get("tag") or [])
    ntype = "Attribution" if "award" in tags else ("Préinformation" if "planning" in tags else "Avis de marché")
    return make_item("Find a Tender (UK)", str(ref), title, buyer, "GBR", to_date(rel.get("date")),
                     to_date((tender.get("tenderPeriod") or {}).get("endDate")), link, ntype,
                     sorted(set(cpvs)), reasons, targets, description=descr, score=score)


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
            reasons, targets, score, keep = matcher.evaluate(title, "", buyer, [])
            if not keep:
                continue
            items.append(make_item("SAM.gov (US)", o.get("noticeId", title), title, buyer, "USA",
                                   to_date(o.get("postedDate")), to_date(o.get("responseDeadLine")),
                                   o.get("uiLink", ""), o.get("type", ""), [], reasons, targets, score=score))
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
                if not matcher.is_tender_title(title):
                    continue  # l'article ne parle pas d'appel d'offres : ignoré
                reasons, targets, score, _ = matcher.evaluate(title, "", "", [])
                items.append(make_item("Presse", link, title, src, "", published, "", link,
                                       "Article", [], reasons or [f"recherche : {q}"], targets,
                                       kind="signal", score=score))
            time.sleep(1)
    return items, []


# ---------------------------------------------------------------------
# Source 6 : sites surveillés (Dematis, plateformes d'achat, sites de prospects)
#   Ouvre chaque page, liste ses liens, et signale les liens nouveaux qui
#   ressemblent à une consultation.
# ---------------------------------------------------------------------
from html.parser import HTMLParser

TENDER_HINT = re.compile(
    r"appel|offre|consultation|tender|rfp|rfq|rfi|\beoi\b|procurement|marche|annonce|avis|"
    r"\bbid|solicitation|expression of interest|\bami\b|dce|cahier des charges", re.I)


class _LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links, self._href, self._text = [], None, []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.links.append((self._href, re.sub(r"\s+", " ", " ".join(self._text)).strip()))
            self._href = None


def extract_links(page_html: str, base_url: str) -> list[tuple[str, str]]:
    parser = _LinkParser()
    try:
        parser.feed(page_html)
    except Exception:
        pass
    seen, out = set(), []
    for href, text in parser.links:
        if not href or href.startswith(("#", "mailto:", "javascript:", "tel:")):
            continue
        absolute = urllib.parse.urljoin(base_url, href.strip())
        if absolute in seen:
            continue
        seen.add(absolute)
        out.append((absolute, html.unescape(text)))
    return out


def fetch_pages(cfg, state, matcher):
    items, warnings = [], []
    memory = state.setdefault("pages_vues", {})
    pages = cfg.get("pages_a_surveiller") or []
    if not pages:
        return None, ["Aucun site dans pages_a_surveiller (config.yaml)."]
    ok_pages = 0
    for page in pages:
        url, name = page.get("url", ""), page.get("nom") or page.get("url", "")
        mode = page.get("filtre", "mots_cles")
        motif = page.get("motif_lien")
        compte = page.get("compte")
        try:
            r = HTTP.get(url, timeout=TIMEOUT)
            r.raise_for_status()
        except Exception as e:
            warnings.append(f"{name} : {type(e).__name__} {str(e)[:80]}")
            continue
        ok_pages += 1
        links = extract_links(r.text, r.url)
        already = set(memory.get(url, []))
        first_visit = url not in memory
        for href, text in links:
            if motif and motif not in href:
                continue
            label = text or urllib.parse.unquote(href.rstrip("/").rsplit("/", 1)[-1])
            if len(label) < 6:
                continue
            reasons, targets, score, keep = matcher.evaluate(label, "", compte or "", [])
            if mode == "tout":
                keep = bool(motif) or bool(TENDER_HINT.search(norm(label + " " + href)))
                reasons = reasons or ["nouvelle consultation sur le site"]
            # sinon : règle normale (seuil, ou 1 point suffit si la page est rattachée à un compte cible)
            if not keep:
                continue
            if compte and compte not in targets:
                targets.append(compte)
            it = make_item(f"Site : {name}", href, label, compte or name, "", "", "", href,
                           "Consultation", [], reasons, targets, score=score)
            if first_visit or href in already:
                # déjà présent lors d'une visite précédente : pas marqué "Nouveau"
                it["vu_le"] = (TODAY - dt.timedelta(days=8)).isoformat()
            items.append(it)
        memory[url] = [h for h, _ in links][:3000]
        time.sleep(1)
    if ok_pages == 0:
        raise RuntimeError(" | ".join(warnings))
    return items, warnings


# ---------------------------------------------------------------------
# Assemblage
# ---------------------------------------------------------------------
SOURCES = [
    ("ted", "TED", "Europe : Journal officiel de l'UE", fetch_ted),
    ("boamp", "BOAMP", "France : marchés publics", fetch_boamp),
    ("find_a_tender", "Find a Tender (UK)", "Royaume-Uni", fetch_fts),
    ("sam_gov", "SAM.gov (US)", "États-Unis : marchés fédéraux", fetch_sam),
    ("sites", "Sites surveillés", "Dematis, plateformes d'achat, sites de prospects", fetch_pages),
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
        elif it.get("vu_le"):
            pass  # date fixée par la source (ex. 1re visite d'un site surveillé)
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

    # Les réglages s'appliquent aussi à l'historique : on re-trie les avis déjà enregistrés
    refiltered = []
    for it in existing:
        reasons, targets, score, keep = matcher.evaluate(it.get("titre", ""), it.get("resume", ""),
                                                         it.get("acheteur", ""), it.get("cpv", []))
        if it.get("nature") == "signal":
            keep = matcher.is_tender_title(it.get("titre", ""))
            reasons = reasons or it.get("pourquoi", [])
        elif str(it.get("source", "")).startswith("Site :") and "nouvelle consultation sur le site" in it.get("pourquoi", []):
            keep, reasons = True, it.get("pourquoi", [])  # page en filtre "tout"
        if keep:
            it.update({"pourquoi": reasons[:6], "score": score,
                       "comptes": sorted(set(targets) | set(it.get("comptes", [])) if targets else it.get("comptes", []))})
            refiltered.append(it)
    if len(refiltered) != len(existing):
        log(f"Historique re-trié avec les réglages actuels : {len(existing) - len(refiltered)} avis retiré(s)")
    items = merge(refiltered, collected, int(cfg.get("conservation_jours", 400)))
    state["dernier_passage"] = NOW_ISO
    save_json(DATA_FILE, items)
    save_json(STATE_FILE, state)
    build_page(cfg, items, state)
    log(f"Page générée : {len(items)} avis au total → {OUTPUT_FILE.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
