#!/usr/bin/env python3
"""
Bison Futé (réseau routier national non concédé) :
  - débits toutes les 6 min (DATEX II)  -> trafic journalier moyen ESTIMÉ par capteur
  - part de poids lourds si le flux la fournit (sinon vide, jamais inventée)
  - position : début/fin du tronçon (référentiel) ; sinon position du capteur
  - autoroutes exclues

Usage :
  python scripts/extract_bisonfute.py            # collecte un relevé (à lancer toutes les 6 min)
  python scripts/extract_bisonfute.py --inspect  # affiche la structure réelle des fichiers
"""
import csv, io, json, os, re, sys, unicodedata
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from pyproj import Transformer

URL_DEBITS = os.environ.get("URL_DEBITS", "https://transport.data.gouv.fr/resources/79165/download")
URL_REF = os.environ.get("URL_REF", "https://transport.data.gouv.fr/resources/79167/download")
STATE, HIST, CURRENT = "data/accumulateurs.json", "data/historique.csv", "data/jour_en_cours.csv"
TZ = ZoneInfo("Europe/Paris")
RELEVES_PAR_JOUR = 240  # 24 h / 6 min
AUTOROUTE = re.compile(r"^\s*A\s?-?\d+", re.I)
L93_TO_GPS = Transformer.from_crs("EPSG:2154", "EPSG:4326", always_xy=True)
FIELDS = ["date", "site_id", "route", "type_localisation", "longueur_troncon_m",
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


def l93_to_gps(x, y):
    """Lambert 93 (mètres) -> (lat, lon) WGS84. None si absent ou hors France métropolitaine."""
    x, y = num(x), num(y)
    if x is None or y is None or not (100000 <= x <= 1300000 and 6000000 <= y <= 7200000):
        return None
    lon, lat = L93_TO_GPS.transform(x, y)
    return round(lat, 6), round(lon, 6)


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
    """-> (lignes sous forme de dict, noms de colonnes, note éventuelle sur un décalage)."""
    text = None
    for enc in ("utf-8-sig", "latin-1"):
        try:
            text = content.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    dialect = csv.Sniffer().sniff(text[:5000], delimiters=";,\t|")
    table = list(csv.reader(io.StringIO(text), dialect=dialect))
    header, data = [h.strip() for h in table[0]], [r for r in table[1:] if r]
    if not data:
        return [], header, None
    note = None
    # Constaté : l'en-tête compte une colonne de plus que les lignes de données,
    # et la colonne absente des données est 'code_insee_commune'.
    if Counter(len(r) for r in data).most_common(1)[0][0] == len(header) - 1 \
            and "code_insee_commune" in header:
        header.remove("code_insee_commune")
        note = "En-tête du référentiel décalé d'une colonne : 'code_insee_commune' ignorée."
    return [dict(zip(header, r)) for r in data], header, note


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
    best, best_n = None, 0  # colonne d'id = celle qui recoupe le mieux les ids du flux
    for col in cols:
        n = len({(r.get(col) or "").strip() for r in rows} & site_ids)
        if n > best_n:
            best, best_n = col, n
    c["id"] = env.get("COL_ID") or best
    c["route"] = env.get("COL_ROUTE") or col_with(cols, [r"\b(route|axe|ligne)\b"])
    c["x_deb"] = col_with(cols, [r"^x deb"])
    c["y_deb"] = col_with(cols, [r"^y deb"])
    c["x_fin"] = col_with(cols, [r"^x fin"])
    c["y_fin"] = col_with(cols, [r"^y fin"])
    c["longueur"] = col_with(cols, [r"^longueur"])
    # repli si un référentiel fournit directement lat/lon (WGS84)
    c["lat"] = env.get("COL_LAT") or col_with(cols, [r"\blat(itude)?\b"], [DEB, FIN])
    c["lon"] = env.get("COL_LON") or col_with(cols, [r"\b(lon|lng|long)(itude)?\b"], [DEB, FIN])
    return c, best_n


def inspect(debits_xml, ref_content):
    snap = parse_debits(debits_xml)
    root = ET.fromstring(debits_xml)
    kinds = sorted({txt(e) for e in root.iter() if ln(e.tag) == "vehicleType"} - {None})
    types = Counter(attr(e, "type") for e in root.iter() if ln(e.tag) == "basicData")
    sample = next((e for e in root.iter() if ln(e.tag) == "siteMeasurements"), None)
    rows, cols, note = read_ref(ref_content)
    cmap, overlap = detect_columns(cols, rows, set(snap))
    gps = sum(1 for r in rows if cmap["x_deb"] and l93_to_gps(r.get(cmap["x_deb"]), r.get(cmap["y_deb"])))
    routes = sum(1 for r in rows if cmap["route"] and (r.get(cmap["route"]) or "").strip())
    autor = sum(1 for r in rows if cmap["route"] and AUTOROUTE.match(r.get(cmap["route"]) or ""))
    lines = [
        "## Inspection des fichiers Bison Futé",
        f"- Sites avec débit exploitable : {len(snap)} (dont {sum(1 for s in snap.values() if s['pl'] is not None)} avec part PL)",
        f"- Types de données dans le flux (basicData) : {dict(types)}",
        f"- vehicleType rencontrés : {kinds or 'aucun'}",
        f"- Colonnes du référentiel : {cols}",
        f"- Note : {note or 'aucun décalage détecté'}",
        f"- Colonnes détectées : {cmap} (ids en commun avec le flux : {overlap})",
        f"- Lignes du référentiel : {len(rows)} | avec route : {routes} | autoroutes : {autor} | avec coordonnées valides : {gps}",
        f"- 2 premières lignes du référentiel : {rows[:2]}",
        "- Premières lignes brutes du fichier CSV :",
        "```", *ref_content.decode("utf-8", "replace").splitlines()[:3], "```",
        "- Exemple brut d'un site du flux de débits :",
        "```", (ET.tostring(sample, encoding="unicode")[:2500] if sample is not None else "aucun"), "```",
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
    rows = []
    cnt = Counter()
    for sid, a in sites.items():
        ref = ref_by_id.get(sid)
        if ref is None:
            cnt["absents du référentiel"] += 1
            continue
        g = lambda k: (ref.get(cmap[k]) or "").strip() if cmap.get(k) else ""
        route = g("route")
        if not route:
            cnt["sans route connue"] += 1  # on ne peut pas affirmer que ce n'est pas une autoroute
            continue
        if AUTOROUTE.match(route):
            cnt["autoroutes"] += 1
            continue
        deb, fin = l93_to_gps(g("x_deb"), g("y_deb")), l93_to_gps(g("x_fin"), g("y_fin"))
        pt = None
        if not deb and cmap.get("lat") and cmap.get("lon"):
            la, lo = num(g("lat")), num(g("lon"))
            pt = (la, lo) if la is not None and lo is not None else None
        if not (deb or pt):
            cnt["sans coordonnées valides"] += 1
            continue
        r = dict.fromkeys(FIELDS, "")
        r.update(date=date, site_id=sid, route=route)
        if deb and fin:
            r.update(type_localisation="troncon", debut_lat=deb[0], debut_lon=deb[1],
                     fin_lat=fin[0], fin_lon=fin[1])
            if num(g("longueur")) is not None:
                r["longueur_troncon_m"] = round(num(g("longueur")))
        else:  # un seul point connu : on l'indique comme position du capteur
            p = deb or pt
            r.update(type_localisation="capteur", capteur_lat=p[0], capteur_lon=p[1])
        r["tmj_estime_veh_jour"] = round(a["sum_rate"] / a["n"] * 24)
        if a["sum_rate_pl"] > 0:
            r["part_pl_pct"] = round(100 * a["sum_pl"] / a["sum_rate_pl"], 2)
            r["source_pl"] = a["pl_source"]
        r.update(nb_releves=a["n"], couverture_pct=min(100, round(100 * a["n"] / RELEVES_PAR_JOUR, 1)),
                 derniere_mesure=a["last_t"] or "")
        rows.append(r)
    print(f"{date}: {len(rows)} lignes écrites | exclus : {dict(cnt)}")
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
    rows, cols, note = read_ref(ref_content)
    if note:
        print(note)
    cmap, overlap = detect_columns(cols, rows, set(snap))
    if not cmap["id"] or not overlap:
        sys.exit(f"Impossible de relier le flux au référentiel. Colonnes : {cols}. "
                 "Lancez --inspect puis fixez COL_ID.")
    if not cmap["route"]:
        sys.exit(f"Pas de colonne route dans le référentiel : exclusion des autoroutes impossible. "
                 f"Colonnes : {cols}. Fixez COL_ROUTE.")
    ref_by_id = {(r.get(cmap["id"]) or "").strip(): r for r in rows}

    today = datetime.now(TZ).strftime("%Y-%m-%d")
    state = load_state(today)
    previous = state.pop("previous", None)
    accumulate(state, snap)
    today_rows = build_rows(today, state["sites"], ref_by_id, cmap)
    if not today_rows:
        sys.exit("Aucune ligne exploitable (route/coordonnées introuvables). Lancez --inspect.")
    if previous:  # la journée précédente est terminée : on l'archive
        write_csv(HIST, build_rows(previous["date"], previous["sites"], ref_by_id, cmap), append=True)

    os.makedirs("data", exist_ok=True)
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(state, f)
    write_csv(CURRENT, today_rows)


if __name__ == "__main__":
    main()
