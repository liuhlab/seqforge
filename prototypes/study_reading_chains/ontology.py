# PROTOTYPE, throwaway (issue #629): ontology term candidates for a label's text.
#
# OLS4 (the EBI's web search): an exact search first (exact=true), then a plain one, merged with
# the exact hits first. Cellosaurus's own API for cell lines. Every response is cached on disk under
# _cache/ontology/, and live requests are spaced to at most 3 per second across threads.
import hashlib
import json
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
CACHE = HERE / "_cache" / "ontology"
_lock = threading.Lock()
_last = [0.0]
STATS = {"live": 0, "cached": 0, "errors": 0}

#: label -> OLS4 ontologies (species-dependent ones resolved by vocab_for).
VOCAB = {
    "tissue": ["uberon"],
    "cell_type": ["cl"],
    "disease": ["mondo"],
    "treatment": ["chebi"],
    "development_stage": None,  # by species
    "cell_line": "cellosaurus",
}
STAGE = {"9606": "hsapdv", "10090": "mmusdv", "6239": "wbls", "7227": "fbdv"}
TISSUE_WORM = "wbbt"


def vocab_for(label, taxid):
    if label == "development_stage":
        v = STAGE.get(str(taxid))
        return [v] if v else None  # rat, fly-less, etc.: no stage vocabulary
    if label == "tissue" and str(taxid) == "6239":
        return [TISSUE_WORM, "uberon"]
    return VOCAB.get(label)


def _get(url):
    key = hashlib.sha1(url.encode()).hexdigest()[:20]
    f = CACHE / f"{key}.json"
    if f.exists():
        STATS["cached"] += 1
        return json.loads(f.read_text())
    with _lock:  # at most 3 requests per second, across threads
        wait = _last[0] + 0.34 - time.time()
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.time()
    req = urllib.request.Request(url, headers={"User-Agent": "seqforge-prototype/0 (python urllib)",
                                               "Accept": "application/json"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.loads(r.read())
            break
        except Exception:  # noqa: BLE001
            if attempt == 2:
                STATS["errors"] += 1
                return None
            time.sleep(2 ** attempt)
    STATS["live"] += 1
    CACHE.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"url": url, "data": data}))
    return {"url": url, "data": data}


def ols(text, ontologies, exact, rows=8):
    q = {"q": text, "ontology": ",".join(ontologies), "rows": rows, "type": "class",
         "queryFields": "label,synonym"}
    if exact:
        q["exact"] = "true"
    got = _get("https://www.ebi.ac.uk/ols4/api/search?" + urllib.parse.urlencode(q))
    if not got:
        return []
    out = []
    for d in got["data"].get("response", {}).get("docs", []):
        oid = d.get("obo_id")
        if oid and d.get("is_defining_ontology", True):
            out.append({"id": oid, "label": d.get("label"), "exact": exact})
    return out


def cellosaurus(text, rows=8):
    q = {"q": text, "format": "json", "rows": rows, "fields": "id,ac,sy"}
    got = _get("https://api.cellosaurus.org/search/cell-line?" + urllib.parse.urlencode(q))
    if not got:
        return []
    out = []
    data = got["data"]
    lines = (data.get("Cellosaurus") or {}).get("cell-line-list") or []
    for cl in lines:
        ac = None
        for a in cl.get("accession-list", []):
            if a.get("type") == "primary":
                ac = a.get("value")
        name = None
        for n in cl.get("name-list", []):
            if n.get("type") == "identifier":
                name = n.get("value")
        if ac:
            out.append({"id": f"CVCL:{ac.split('_')[-1]}" if not ac.startswith("CVCL") else ac,
                        "label": name, "exact": (name or "").lower() == text.lower()})
    return out


def candidates(label, text, taxid, cap=8):
    """Up to `cap` candidates: OLS4 exact hits first, then plain hits; Cellosaurus for cell lines."""
    v = vocab_for(label, taxid)
    if not v or not text or not text.strip():
        return [], v
    text = text.strip()[:200]
    if v == "cellosaurus":
        return cellosaurus(text)[:cap], v
    seen, out = set(), []
    for exact in (True, False):
        for c in ols(text, v, exact):
            if c["id"] not in seen:
                seen.add(c["id"])
                out.append(c)
    return out[:cap], v
