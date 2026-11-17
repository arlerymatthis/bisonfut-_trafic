"""
Bison Futé (réseau routier national non concédé) :
  - débits toutes les 6 min (DATEX II)  -> trafic journalier moyen ESTIMÉ par capteur
  - part de poids lourds si le flux la fournit (sinon vide, jamais inventée)
  - position : début/fin du tronçon si le référentiel les donne, sinon position du capteur
  - autoroutes exclues

Usage :
  python scripts/extract_bisonfute.py            # collecte un relevé (à lancer toutes les 6 min)
  python scripts/extract_bisonfute.py --inspect  # affiche la structure réelle des fichiers
"""
import csv, io, json, os, re, sys, unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

URL_DEBITS = os.environ.get("URL_DEBITS", "https://transport.data.gouv.fr/resources/79165/download")
URL_REF = os.environ.get("URL_REF", "https://transport.data.gouv.fr/resources/79167/download")
STATE, HIST, CURRENT = "data/accumulateurs.json", "data/historique.csv", "data/jour_en_cours.csv"
TZ = ZoneInfo("Europe/Paris")
RELEVES_PAR_JOUR = 240  # 24 h / 6 min
AUTOROUTE = re.compile(r"^\s*A\s?-?\d+", re.I)
FIELDS = ["date", "site_id", "route", "type_localisation",
          "debut_lat", "debut_lon", "fin_lat", "fin_lon", "capteur_lat", "capteur_lon",
          "tmj_estime_veh_jour", "part_pl_pct", "source_pl", "nb_releves",
          "couverture_pct", "derniere_mesure"]


# ---------- utilitaires ----------
def get(url):
    r = requests.get(url, timeout=120, headers={"User-Agent": "bisonfute-collecteur/1.0"})
    r.raise_for_status()
    return r.content


def ln(tag):
    return tag.rsplit("}", 1)[-1]


def attr(el, name):
    for k, v in el.attrib.items():
        if ln(k) == name:
            return v
    return None


def first(el, name):
    if el is None:
        return None
    return next((e for e in el.iter() if ln(e.tag) == name), None)


def txt(el):
    return el.text.strip() if el is not None and el.text and el.text.strip() else None


def num(s):
    try:
        return float(str(s).replace(",", ".").strip())
    except (TypeError, ValueError):
        return None


def norm(s):
    s = unicodedata.normalize("NFD", str(s)).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


# ---------- flux DATEX II des débits ----------
def parse_debits(content):
    """-> {site_id: {t, rate, pl, pl_src}} pour le relevé courant."""
    root = ET.fromstring(content)
    out = {}
    for sm in root.iter():
        if ln(sm.tag) != "siteMeasurements":
            continue
        ref = first(sm, "measurementSiteReference")
        sid = attr(ref, "id") if ref is not None else None
        if not sid:
            continue
        t = txt(first(sm, "measurementTimeDefault"))
        flows = []
        for bd in sm.iter():
            if ln(bd.tag) != "basicData" or not (attr(bd, "type") or "").endswith("TrafficFlow"):
                continue
            err = any(attr(e, "dataError") == "true" for e in bd.iter())
            rate = num(txt(first(bd, "vehicleFlowRate")))
            pct_el = first(bd, "percentageLongVehicles")
            pct = num(txt(pct_el) or txt(first(pct_el, "percentage")))
            vt = txt(first(bd, "vehicleType"))
            if not err and rate is not None and rate >= 0:
                flows.append({"rate": rate, "pct": pct, "vt": vt})
        tot = [f for f in flows if f["vt"] in (None, "anyVehicle")]
        if not tot:
            continue
        total = sum(f["rate"] for f in tot)
        lorry = [f for f in flows if f["vt"] == "lorry"]
        pl, src = None, None
        if lorry and total > 0:
            pl, src = 100 * sum(f["rate"] for f in lorry) / total, "debit_lorry"
        else:
            with_pct = [f for f in tot if f["pct"] is not None]
            w = sum(f["rate"] for f in with_pct)
            if with_pct and w > 0:
                pl = sum(f["rate"] * f["pct"] for f in with_pct) / w
                src = "percentageLongVehicles"
        out[sid] = {"t": t, "rate": total, "pl": pl, "pl_src": src}
    return out


# ---------- référentiel des stations ----------
def read_ref(content):
    for enc in ("utf-8-sig", "latin-1"):
        try:
            text = content.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    dialect = csv.Sniffer().sniff(text[:5000], delimiters=";,\t|")
    rows = list(csv.DictReader(io.StringIO(text), dialect=dialect))
    return rows, list(rows[0].keys()) if rows else []


def col_with(cols, must, none=()):
    for c in cols:
        n = norm(c)
        if all(re.search(p, n) for p in must) and not any(re.search(p, n) for p in none):
            return c
    return None


DEB, FIN = r"deb|start|origin|amont|from", r"\bfin\b|\bend\b|aval|\bto\b"


def detect_columns(cols, rows, site_ids):
    env = os.environ
    c = {}
    # colonne d'identifiant = celle qui recoupe le mieux les ids du flux de débits
    best, best_n = None, 0
    for col in cols:
        n = len({(r.get(col) or "").strip() for r in rows} & site_ids)
        if n > best_n:
            best, best_n = col, n
    c["id"] = env.get("COL_ID") or best
    c["route"] = env.get("COL_ROUTE") or col_with(cols, [r"\b(route|axe|ligne)\b"], [r"voie"])
    c["lat"] = env.get("COL_LAT") or col_with(cols, [r"\blat"], [DEB, FIN])
    c["lon"] = env.get("COL_LON") or col_with(cols, [r"\b(lon|lng)"], [DEB, FIN])
    c["lat_deb"] = col_with(cols, [r"\blat", DEB])
    c["lon_deb"] = col_with(cols, [r"\b(lon|lng)", DEB])
    c["lat_fin"] = col_with(cols, [r"\blat", FIN])
    c["lon_fin"] = col_with(cols, [r"\b(lon|lng)", FIN])
    return c, best_n


def inspect(debits_xml, ref_content):
    snap = parse_debits(debits_xml)
    root = ET.fromstring(debits_xml)
    kinds = sorted({txt(e) for e in root.iter() if ln(e.tag) == "vehicleType"} - {None})
    rows, cols = read_ref(ref_content)
    cmap, overlap = detect_columns(cols, rows, set(snap))
    pl_n = sum(1 for s in snap.values() if s["pl"] is not None)
    lines = [
        "## Inspection des fichiers Bison Futé",
        f"- Sites avec débit exploitable : {len(snap)} (dont {pl_n} avec part PL)",
        f"- vehicleType rencontrés : {kinds or 'aucun'}",
        f"- Exemple de ids du flux : {list(snap)[:5]}",
        f"- Colonnes du référentiel : {cols}",
        f"- Colonnes détectées : {cmap} (ids en commun avec le flux : {overlap})",
        f"- 2 premières lignes du référentiel : {rows[:2]}",
    ]
    print("\n".join(lines))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


# ---------- accumulation ----------
def load_state(today):
    if os.path.exists(STATE):
        with open(STATE, encoding="utf-8") as f:
            s = json.load(f)
        if s.get("date") == today:
            return s
        return {"date": today, "sites": {}, "previous": s}
    return {"date": today, "sites": {}}


def accumulate(state, snap):
    for sid, s in snap.items():
        a = state["sites"].setdefault(sid, {"n": 0, "sum_rate": 0.0, "sum_pl": 0.0,
                                            "sum_rate_pl": 0.0, "last_t": None, "pl_source": None})
        if s["t"] and a["last_t"] == s["t"]:
            continue  # même mesure que le relevé précédent
        a["n"] += 1
        a["sum_rate"] += s["rate"]
        a["last_t"] = s["t"]
        if s["pl"] is not None:
            a["sum_pl"] += s["rate"] * s["pl"] / 100
            a["sum_rate_pl"] += s["rate"]
            a["pl_source"] = s["pl_src"]


def build_rows(date, sites, ref_by_id, cmap):
    rows, no_ref, no_route, motorway = [], 0, 0, 0
    for sid, a in sites.items():
        ref = ref_by_id.get(sid)
        if ref is None:
            no_ref += 1
            continue
        route = (ref.get(cmap["route"]) or "").strip()
        if not route:
            no_route += 1  # on ne peut pas affirmer que ce n'est pas une autoroute
            continue
        if AUTOROUTE.match(route):
            motorway += 1
            continue
        g = lambda k: (ref.get(cmap[k]) or "").strip() if cmap.get(k) else ""
        has_section = all(g(k) for k in ("lat_deb", "lon_deb", "lat_fin", "lon_fin"))
        r = dict.fromkeys(FIELDS, "")
        r.update(date=date, site_id=sid, route=route,
                 type_localisation="troncon" if has_section else "capteur")
        if has_section:
            r.update(debut_lat=g("lat_deb"), debut_lon=g("lon_deb"),
                     fin_lat=g("lat_fin"), fin_lon=g("lon_fin"))
        else:
            r.update(capteur_lat=g("lat"), capteur_lon=g("lon"))
        r["tmj_estime_veh_jour"] = round(a["sum_rate"] / a["n"] * 24)
        if a["sum_rate_pl"] > 0:
            r["part_pl_pct"] = round(100 * a["sum_pl"] / a["sum_rate_pl"], 2)
            r["source_pl"] = a["pl_source"]
        r.update(nb_releves=a["n"], couverture_pct=min(100, round(100 * a["n"] / RELEVES_PAR_JOUR, 1)),
                 derniere_mesure=a["last_t"] or "")
        rows.append(r)
    print(f"{date}: {len(rows)} lignes | exclus : {motorway} autoroutes, "
          f"{no_route} sans route connue, {no_ref} absents du référentiel")
    return rows


def write_csv(path, rows, append=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    new = not (append and os.path.exists(path))
    with open(path, "a" if append else "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerows(rows)


def main():
    debits_xml, ref_content = get(URL_DEBITS), get(URL_REF)
    if "--inspect" in sys.argv:
        return inspect(debits_xml, ref_content)

    snap = parse_debits(debits_xml)
    rows, cols = read_ref(ref_content)
    cmap, overlap = detect_columns(cols, rows, set(snap))
    if not cmap["id"] or not overlap:
        sys.exit(f"Impossible de relier le flux au référentiel. Colonnes : {cols}. "
                 "Lancez --inspect puis fixez COL_ID.")
    if not cmap["route"]:
        sys.exit(f"Pas de colonne 'route' dans le référentiel : exclusion des autoroutes impossible. "
                 f"Colonnes : {cols}. Fixez COL_ROUTE.")
    if not (cmap["lat"] and cmap["lon"]) and not cmap["lat_deb"]:
        sys.exit(f"Pas de coordonnées trouvées. Colonnes : {cols}. Fixez COL_LAT / COL_LON.")
    ref_by_id = {(r.get(cmap["id"]) or "").strip(): r for r in rows}

    today = datetime.now(TZ).strftime("%Y-%m-%d")
    state = load_state(today)
    previous = state.pop("previous", None)
    if previous:  # la journée précédente est terminée : on l'archive
        write_csv(HIST, build_rows(previous["date"], previous["sites"], ref_by_id, cmap), append=True)
    accumulate(state, snap)

    os.makedirs("data", exist_ok=True)
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(state, f)
    write_csv(CURRENT, build_rows(today, state["sites"], ref_by_id, cmap))


if __name__ == "__main__":
    main()
