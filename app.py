"""
US Ballot Lookup - Flask web app
=================================
Enter a ZIP code, see every candidate race and referendum/ballot measure
for your county and state.

Single-file app: Flask + SQLite + Ballotpedia scraper + embedded HTML templates.

Run locally:
    pip install -r requirements.txt
    python app.py

Deployed (recommended): see the deployment guide. GitHub holds the code,
Render runs the Flask server + SQLite database in the cloud.
"""

import os
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup
from flask import Flask, g, jsonify, redirect, render_template_string, request, url_for

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DATABASE_URL", os.path.join(APP_DIR, "ballot.db"))
USER_AGENT = "USBallotLookup/1.0 (civic information project)"

app = Flask(__name__)


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

SCHEMA = """
DROP TABLE IF EXISTS candidates;
DROP TABLE IF EXISTS referendums;
DROP TABLE IF EXISTS offices;
DROP TABLE IF EXISTS zips;
DROP TABLE IF EXISTS counties;
DROP TABLE IF EXISTS states;
DROP TABLE IF EXISTS scrape_log;

CREATE TABLE states (
    abbr       TEXT PRIMARY KEY,          -- 'CA'
    name       TEXT NOT NULL,             -- 'California'
    ballot_url TEXT                       -- Ballotpedia state elections page
);

CREATE TABLE counties (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    state_abbr TEXT NOT NULL REFERENCES states(abbr),
    name       TEXT NOT NULL,             -- 'Los Angeles County'
    ballot_url TEXT,                      -- Ballotpedia county page
    last_sync  TEXT,                      -- last successful scrape (ISO 8601)
    UNIQUE (state_abbr, name)
);

-- One row per 5-digit ZIP; a ZIP can span multiple counties.
CREATE TABLE zips (
    zip        TEXT NOT NULL,
    state_abbr TEXT NOT NULL REFERENCES states(abbr),
    county_id  INTEGER NOT NULL REFERENCES counties(id),
    PRIMARY KEY (zip, county_id)
);

CREATE TABLE offices (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    county_id  INTEGER REFERENCES counties(id),  -- NULL = statewide office
    state_abbr TEXT NOT NULL REFERENCES states(abbr),
    title      TEXT NOT NULL,             -- 'U.S. Senate', 'Sheriff', 'Mayor'
    level      TEXT NOT NULL,             -- federal | state | county | city
    district   TEXT                      -- e.g. 'District 3'
);

CREATE TABLE candidates (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    office_id  INTEGER NOT NULL REFERENCES offices(id),
    name       TEXT NOT NULL,
    party      TEXT,
    ballot_url TEXT,                      -- Ballotpedia profile
    photo_url  TEXT,
    website    TEXT
);

CREATE TABLE referendums (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    county_id   INTEGER REFERENCES counties(id), -- NULL = statewide measure
    state_abbr  TEXT NOT NULL REFERENCES states(abbr),
    title       TEXT NOT NULL,
    description TEXT,
    measure     TEXT,                     -- 'Proposition 1', 'Measure A'
    position    TEXT,                     -- e.g. topic area
    yes_votes   INTEGER,
    no_votes    INTEGER,
    status      TEXT,                     -- approved | defeated | pending
    ballot_url  TEXT
);

CREATE TABLE scrape_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    target     TEXT NOT NULL,             -- 'county:CA:Los Angeles County'
    source     TEXT NOT NULL,             -- 'ballotpedia'
    ok         INTEGER NOT NULL,          -- 1 success, 0 failure
    message    TEXT,
    ran_at     TEXT NOT NULL
);
"""


def get_db() -> sqlite3.Connection:
    """One connection per request, row access by name."""
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    """Create tables and seed states. Safe to run on every boot."""
    db = sqlite3.connect(DB_PATH)
    db.executescript(SCHEMA)
    db.executemany(
        "INSERT OR REPLACE INTO states (abbr, name, ballot_url) VALUES (?,?,?)",
        seed_states(),
    )
    db.commit()
    db.close()


def seed_states():
    base = "https://ballotpedia.org/{}_elections"
    raw = [
        ("AL", "Alabama"), ("AK", "Alaska"), ("AZ", "Arizona"), ("AR", "Arkansas"),
        ("CA", "California"), ("CO", "Colorado"), ("CT", "Connecticut"),
        ("DE", "Delaware"), ("FL", "Florida"), ("GA", "Georgia"), ("HI", "Hawaii"),
        ("ID", "Idaho"), ("IL", "Illinois"), ("IN", "Indiana"), ("IA", "Iowa"),
        ("KS", "Kansas"), ("KY", "Kentucky"), ("LA", "Louisiana"), ("ME", "Maine"),
        ("MD", "Maryland"), ("MA", "Massachusetts"), ("MI", "Michigan"),
        ("MN", "Minnesota"), ("MS", "Mississippi"), ("MO", "Missouri"),
        ("MT", "Montana"), ("NE", "Nebraska"), ("NV", "Nevada"),
        ("NH", "New Hampshire"), ("NJ", "New Jersey"), ("NM", "New Mexico"),
        ("NY", "New York"), ("NC", "North Carolina"), ("ND", "North Dakota"),
        ("OH", "Ohio"), ("OK", "Oklahoma"), ("OR", "Oregon"),
        ("PA", "Pennsylvania"), ("RI", "Rhode Island"), ("SC", "South Carolina"),
        ("SD", "South Dakota"), ("TN", "Tennessee"), ("TX", "Texas"),
        ("UT", "Utah"), ("VT", "Vermont"), ("VA", "Virginia"),
        ("WA", "Washington"), ("WV", "West Virginia"), ("WI", "Wisconsin"),
        ("WY", "Wyoming"), ("DC", "District of Columbia"),
    ]
    out = []
    for abbr, name in raw:
        url = base.format(name.replace(" ", "_")) + ",_{}".format(abbr)
        out.append((abbr, name, url))
    return out


# ---------------------------------------------------------------------------
# ZIP code -> county mapping
#
# At boot we download the HUD USPS ZIP-County crosswalk (public, keyless) and
# fall back to a small embedded sample so the app runs even offline.
# Crosswalk: https://www.huduser.gov/hudapi/public/rest/usps/type/counties
# (HUD requires a free token in production; we use the Census ZCTA relationship
#  file below, which is keyless.)
# ---------------------------------------------------------------------------

ZCTA_COUNTY_URL = (
    "https://www2.census.gov/geo/docs/maps-data/data/rel2020/zcta520/tab20_zcta520_county20_natl.txt"
)

SAMPLE_ZIPS = [
    # (zip, state, county name)
    ("90001", "CA", "Los Angeles County"),
    ("90210", "CA", "Los Angeles County"),
    ("94105", "CA", "San Francisco County"),
    ("10001", "NY", "New York County"),
    ("10002", "NY", "Kings County"),
    ("60601", "IL", "Cook County"),
    ("33101", "FL", "Miami-Dade County"),
    ("73301", "TX", "Travis County"),
    ("80202", "CO", "City and County of Denver"),
    ("98101", "WA", "King County"),
]


def _county_key(name: str) -> str:
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", n.lower())


def load_zip_data(db: sqlite3.Connection):
    """Load ZCTA -> county rows from the Census 2020 relationship file.

    File layout (pipe-delimited, first line is a header):
      col 0  OID_ZCTA5_20        (may be blank)
      col 1  GEOID_ZCTA5_20      5-digit ZCTA  <-- the ZIP
      col 9  GEOID_COUNTY_20     5-digit county GEOID (state FIPS + county FIPS)
      col 10 NAMELSAD_COUNTY_20  e.g. 'Los Angeles County'
    Rows with a blank ZCTA are county-total lines and are skipped.
    """
    rows = []
    try:
        resp = requests.get(ZCTA_COUNTY_URL, timeout=120,
                            headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
        for line in resp.text.splitlines():
            parts = [p.strip() for p in line.split("|")]
            if len(parts) < 11:
                continue
            zcta, geoid, county_name = parts[1], parts[9], parts[10]
            if not zcta.isdigit() or len(zcta) != 5 or len(geoid) != 5:
                continue
            rows.append((zcta, geoid[:2], geoid[2:], county_name))
    except Exception as exc:  # offline / blocked -> sample data
        rows = []
        print("ZIP crosswalk download failed ({}), using sample ZIPs".format(exc))

    if not rows:
        for zc, st, county in SAMPLE_ZIPS:
            cid = _ensure_county(db, st, county)
            db.execute("INSERT OR IGNORE INTO zips VALUES (?,?,?)", (zc, st, cid))
        db.commit()
        return

    for zc, state_fips, county_fips, county_name in rows:
        st = FIPS_STATE.get(state_fips)
        if st is None:
            continue
        cid = _ensure_county(db, st, county_name)
        db.execute("INSERT OR IGNORE INTO zips VALUES (?,?,?)", (zc, st, cid))
    db.commit()


COUNTY_NAMES_URL = (
    "https://www2.census.gov/geo/docs/reference/codes2020/national_county.txt"
)

FIPS_STATE = {
    "01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA", "08": "CO",
    "09": "CT", "10": "DE", "11": "DC", "12": "FL", "13": "GA", "15": "HI",
    "16": "ID", "17": "IL", "18": "IN", "19": "IA", "20": "KS", "21": "KY",
    "22": "LA", "23": "ME", "24": "MD", "25": "MA", "26": "MI", "27": "MN",
    "28": "MS", "29": "MO", "30": "MT", "31": "NE", "32": "NV", "33": "NH",
    "34": "NJ", "35": "NM", "36": "NY", "37": "NC", "38": "ND", "39": "OH",
    "40": "OK", "41": "OR", "42": "PA", "44": "RI", "45": "SC", "46": "SD",
    "47": "TN", "48": "TX", "49": "UT", "50": "VT", "51": "VA", "53": "WA",
    "54": "WV", "55": "WI", "56": "WY",
}


def _fetch_county_names():
    """(state_fips, county_fips) -> 'Los Angeles County'"""
    names = {}
    try:
        resp = requests.get(COUNTY_NAMES_URL, timeout=60,
                            headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
        for line in resp.text.splitlines():
            parts = [p.strip() for p in line.split("|")]
            # STATE|STATEFP|COUNTYFP|COUNTYNAME|CLASSFP
            if len(parts) >= 4 and parts[0] in FIPS_STATE:
                names[(parts[1], parts[2])] = parts[3]
    except Exception as exc:
        print("County names download failed: {}".format(exc))
    return names


def _ensure_county(db: sqlite3.Connection, state_abbr: str, name: str) -> int:
    slug = "{},_{}".format(name.replace(" ", "_"), state_abbr)
    url = "https://ballotpedia.org/{}".format(slug)
    cur = db.execute(
        "INSERT OR IGNORE INTO counties (state_abbr, name, ballot_url) VALUES (?,?,?)",
        (state_abbr, name, url),
    )
    if cur.lastrowid and cur.rowcount:
        row = db.execute("SELECT id FROM counties WHERE state_abbr=? AND name=?",
                         (state_abbr, name)).fetchone()
        return row["id"]
    row = db.execute("SELECT id FROM counties WHERE state_abbr=? AND name=?",
                     (state_abbr, name)).fetchone()
    return row["id"]


# ---------------------------------------------------------------------------
# Ballotpedia scraper
#
# NOTE: Ballotpedia is wiki software (MediaWiki). Its pages change and this
# scraper is intentionally defensive: anything it cannot parse is skipped,
# never fatal. For production-grade reliability, consider MediaWiki's
# API (ballotpedia.org/api.php) which is more stable than HTML scraping.
# ---------------------------------------------------------------------------

def _bp_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    return s


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def scrape_county(state_abbr: str, county_name: str) -> dict:
    """
    Scrape a county's Ballotpedia election page for races and measures.
    Returns {'races': [(office, level, [(name, party, url)...])],
             'measures': [(measure, title, description)]}
    """
    slug_county = county_name.replace(" ", "_")
    url = "https://ballotpedia.org/{},_{}".format(slug_county, state_abbr)
    result = {"races": [], "measures": []}
    sess = _bp_session()
    try:
        html = sess.get(url, timeout=30).text
    except Exception as exc:
        try:
            _log(get_db(), "county:{}:{}".format(state_abbr, county_name), 0, str(exc))
        except Exception:
            pass
        return result
    soup = BeautifulSoup(html, "html.parser")

    # Ballot measure tables usually carry the word "ballot measure" in a
    # preceding heading or the table class contains 'measure'.
    for table in soup.find_all("table"):
        header_txt = _clean(table.get_text(" ", strip=True))[:200].lower()
        if "measure" in header_txt or "proposition" in header_txt or "question" in header_txt:
            for tr in table.find_all("tr"):
                cells = tr.find_all(["td", "th"])
                if len(cells) >= 2:
                    measure = _clean(cells[0].get_text())
                    title = _clean(cells[1].get_text())
                    link = cells[0].find("a")
                    m_url = "https://ballotpedia.org" + link["href"] if (link and link.get("href")) else None
                    if measure and title:
                        result["measures"].append((measure, title, m_url))

    # Candidate rows: links inside tables with a party marker nearby.
    for tr in soup.find_all("tr"):
        links = tr.find_all("a", href=True)
        if not links:
            continue
        text = _clean(tr.get_text(" ", strip=True))
        m = re.search(r"\((Democratic|Republican|Libertarian|Green|Nonpartisan|Independent|No party)[^)]*\)", text, re.I)
        if not m:
            continue
        party = m.group(1)
        # Office is usually the row's first header cell or preceding heading.
        th = tr.find_previous(["h2", "h3"])
        office = _clean(th.get_text()) if th else "Local office"
        for a in links:
            name = _clean(a.get_text())
            if name and "/wiki/" in a["href"] and "," in text and len(name) < 60:
                c_url = "https://ballotpedia.org" + a["href"]
                result["races"].append((office, "local", [(name, party, c_url)]))

    try:
        _log(get_db(), "county:{}:{}".format(state_abbr, county_name), 1,
             "{} races, {} measures".format(len(result["races"]), len(result["measures"])))
    except Exception:
        pass
    return result


def _log(db: sqlite3.Connection, target: str, ok: int, message: str):
    db.execute(
        "INSERT INTO scrape_log (target, source, ok, message, ran_at) VALUES (?,?,?,?,?)",
        (target, "ballotpedia", ok, message,
         datetime.now(timezone.utc).isoformat(timespec="seconds")),
    )
    db.commit()


def sync_county(state_abbr: str, county_name: str):
    """Scrape a county and write results into the database."""
    db = get_db()
    county = db.execute(
        "SELECT id, name FROM counties WHERE state_abbr=? AND name=?",
        (state_abbr, county_name)).fetchone()
    if not county:
        return
    data = scrape_county(state_abbr, county_name)

    for office_title, level, cands in data["races"]:
        cur = db.execute(
            "INSERT INTO offices (county_id, state_abbr, title, level) VALUES (?,?,?,?)",
            (county["id"], state_abbr, office_title, level))
        office_id = cur.lastrowid
        for name, party, url in cands:
            db.execute(
                "INSERT INTO candidates (office_id, name, party, ballot_url) VALUES (?,?,?,?)",
                (office_id, name, party, url))

    for measure, title, m_url in data["measures"]:
        db.execute(
            """INSERT INTO referendums (county_id, state_abbr, measure, title, ballot_url)
               VALUES (?,?,?,?,?)""",
            (county["id"], state_abbr, measure, title, m_url))

    db.execute("UPDATE counties SET last_sync=? WHERE id=?",
               (datetime.now(timezone.utc).isoformat(timespec="seconds"), county["id"]))
    db.commit()


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

def lookup_zip(zip_code: str):
    """All counties for a ZIP plus the state, or None if unknown."""
    db = get_db()
    rows = db.execute(
        """SELECT z.zip, z.state_abbr, c.id AS county_id, c.name AS county_name,
                  s.name AS state_name
           FROM zips z
           JOIN counties c ON c.id = z.county_id
           JOIN states s ON s.abbr = z.state_abbr
           WHERE z.zip = ?""",
        (zip_code,)).fetchall()
    return rows or None


def ballot_for(state_abbr: str, county_id: int):
    """Races and measures for one county + statewide offices/measures."""
    db = get_db()
    offices = db.execute(
        """SELECT o.id, o.title, o.level, o.district FROM offices o
           WHERE o.county_id = ?
           ORDER BY CASE o.level WHEN 'federal' THEN 1 WHEN 'state' THEN 2
                                  WHEN 'county' THEN 3 ELSE 4 END, o.title""",
        (county_id,)).fetchall()
    races = []
    for office in offices:
        cands = db.execute(
            "SELECT name, party, ballot_url FROM candidates WHERE office_id=? ORDER BY name",
            (office["id"],)).fetchall()
        races.append({"office": office, "candidates": cands})

    measures = db.execute(
        """SELECT measure, title, description, status, yes_votes, no_votes, ballot_url
           FROM referendums WHERE county_id=? OR county_id IS NULL
           ORDER BY county_id IS NULL DESC, measure""",
        (county_id,)).fetchall()
    return races, measures


# ---------------------------------------------------------------------------
# Templates (plain HTML/CSS, no frameworks)
# ---------------------------------------------------------------------------

BASE_CSS = """
:root { --ink:#1a2333; --muted:#5b6474; --line:#e3e7ee; --accent:#1d4ed8; --bg:#f6f7f9; }
* { box-sizing:border-box; }
body { font-family:Georgia,'Times New Roman',serif; margin:0; color:var(--ink); background:var(--bg); }
.wrap { max-width:860px; margin:0 auto; padding:24px 20px 64px; }
header.site { border-bottom:3px double var(--ink); padding:18px 0; margin-bottom:28px; }
header.site h1 { font-size:1.7rem; margin:0; letter-spacing:.5px; }
header.site p { margin:4px 0 0; color:var(--muted); font-size:.95rem; }
form.zip { display:flex; gap:10px; margin:26px 0; }
form.zip input { font-size:1.15rem; padding:10px 14px; border:1px solid var(--line); border-radius:6px; width:180px; font-family:inherit; }
form.zip button { font-size:1.05rem; padding:10px 22px; background:var(--accent); color:#fff; border:0; border-radius:6px; cursor:pointer; font-family:inherit; }
form.zip button:hover { background:#173db0; }
h2 { font-size:1.25rem; border-bottom:1px solid var(--line); padding-bottom:6px; }
h3 { font-size:1.05rem; margin:22px 0 6px; }
.race { background:#fff; border:1px solid var(--line); border-radius:8px; padding:14px 18px; margin:10px 0; }
.race .office { font-weight:bold; font-size:1.02rem; }
.race .office small { color:var(--muted); font-weight:normal; margin-left:8px; }
.cand { display:flex; justify-content:space-between; padding:6px 0; border-top:1px dotted var(--line); }
.cand:first-of-type { border-top:0; }
.party { color:var(--muted); }
.dem { color:#2560a8; } .gop { color:#a83232; } .other { color:#5b6474; }
.measure { background:#fff; border:1px solid var(--line); border-left:4px solid var(--accent); border-radius:6px; padding:12px 16px; margin:10px 0; }
.measure .mname { font-weight:bold; }
.muted { color:var(--muted); }
.error { background:#fdecec; border:1px solid #f2b8b5; color:#7f1d1d; padding:12px 16px; border-radius:6px; }
ul.counties { list-style:none; padding:0; }
ul.counties li { background:#fff; border:1px solid var(--line); border-radius:6px; padding:10px 14px; margin:8px 0; }
ul.counties a { color:var(--accent); text-decoration:none; }
ul.counties a:hover { text-decoration:underline; }
footer { margin-top:48px; color:var(--muted); font-size:.85rem; border-top:1px solid var(--line); padding-top:14px; }
a.back { display:inline-block; margin-bottom:18px; color:var(--accent); text-decoration:none; }
"""

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>US Ballot Lookup</title>
<style>{css}</style></head>
<body><div class="wrap">
<header class="site"><h1>US Ballot Lookup</h1>
<p>Candidates and referendums for every county and state</p></header>
{body}
<footer>Data sourced from Ballotpedia. Candidate and measure information is
provided for informational purposes only; verify with your official sample
ballot from your local election office.</footer>
</div></body></html>"""


def _party_span(party: str) -> str:
    if not party:
        return '<span class="party"></span>'
    cls = "other"
    if "democratic" in party.lower():
        cls = "dem"
    elif "republican" in party.lower():
        cls = "gop"
    return '<span class="party {}">({})</span>'.format(cls, party)


def render(body: str) -> str:
    return PAGE.format(css=BASE_CSS, body=body)


HOME_BODY = """
<p>Enter your ZIP code to see the races and ballot measures for your county
and state, including federal, state, county, and city offices.</p>
<form class="zip" method="get" action="{action}">
  <input type="text" name="zip" inputmode="numeric" pattern="[0-9]{{5}}"
         maxlength="5" placeholder="ZIP code" required>
  <button type="submit">Look up my ballot</button>
</form>
<p class="muted">The database refreshes from Ballotpedia, so results stay
current as candidates qualify and measures are certified.</p>
"""


@app.route("/")
def home():
    return render(HOME_BODY.format(action=url_for("lookup")))


@app.route("/lookup")
def lookup():
    zip_code = (request.args.get("zip") or "").strip()
    if not re.fullmatch(r"\d{5}", zip_code):
        return render('<p class="error">Please enter a valid 5-digit ZIP code.</p>'
                      + HOME_BODY.format(action=url_for("lookup")))
    matches = lookup_zip(zip_code)
    if not matches:
        body = ('<p class="error">No county found for ZIP {}.</p>'.format(zip_code)
                + HOME_BODY.format(action=url_for("lookup")))
        return render(body)

    st = matches[0]["state_abbr"]
    state_name = matches[0]["state_name"]
    parts = ['<h2>{} &middot; ZIP {}</h2>'.format(state_name, zip_code)]
    parts.append("<h3>Matching counties</h3><ul class='counties'>")
    for m in matches:
        parts.append('<li><a href="{}">{} County ballot</a></li>'.format(
            url_for("county_page", state=st, county_id=m["county_id"]),
            m["county_name"]))
    parts.append("</ul>")
    return render("".join(parts))


@app.route("/ballot/<state>/<int:county_id>")
def county_page(state, county_id):
    db = get_db()
    county = db.execute(
        """SELECT c.name, s.name AS state_name FROM counties c
           JOIN states s ON s.abbr = c.state_abbr
           WHERE c.id=? AND c.state_abbr=?""",
        (county_id, state)).fetchone()
    if not county:
        return render('<p class="error">County not found.</p>'), 404

    races, measures = ballot_for(state, county_id)
    parts = ['<a class="back" href="{0}">&larr; New search</a>'.format(url_for("home"))]
    parts.append("<h2>{} &middot; {}</h2>".format(county["name"], county["state_name"]))

    parts.append("<h3>Candidate races</h3>")
    if races:
        for race in races:
            office = race["office"]
            parts.append('<div class="race"><div class="office">{}'
                         '<small>{}</small></div>'.format(
                             office["title"],
                             (office["district"] or "") + " " + office["level"]))
            for c in race["candidates"]:
                name = ('<a href="{}">{}</a>'.format(c["ballot_url"], c["name"])
                        if c["ballot_url"] else c["name"])
                parts.append('<div class="cand"><span>{}</span>{}</div>'.format(
                    name, _party_span(c["party"])))
            parts.append("</div>")
    else:
        parts.append('<p class="muted">No candidate data has been synced for this '
                     'county yet. An administrator can trigger a refresh.</p>')

    parts.append("<h3>Referendums and ballot measures</h3>")
    if measures:
        for m in measures:
            title = ('<a href="{}">{}</a>'.format(m["ballot_url"], m["title"])
                     if m["ballot_url"] else m["title"])
            desc = '<div class="muted">{}</div>'.format(m["description"]) if m["description"] else ""
            parts.append('<div class="measure"><span class="mname">{}</span> {}{}</div>'.format(
                m["measure"] or "Measure", title, desc))
    else:
        parts.append('<p class="muted">No ballot measures on record for this area.</p>')

    return render("".join(parts))


# ---------------------------------------------------------------------------
# Admin / refresh endpoints (protect with ADMIN_TOKEN in production)
# ---------------------------------------------------------------------------

@app.route("/admin/refresh", methods=["POST"])
def admin_refresh():
    token = os.environ.get("ADMIN_TOKEN", "")
    if token and request.args.get("token") != token:
        return jsonify({"error": "forbidden"}), 403
    state = request.args.get("state")
    county = request.args.get("county")
    if state and county:
        sync_county(state.upper(), county)
        return jsonify({"ok": True, "synced": "{}/{}".format(state, county)})
    # No target: refresh the counties belonging to the given ZIP (or sample set)
    zip_code = request.args.get("zip")
    if zip_code:
        matches = lookup_zip(zip_code)
        if matches:
            for m in matches:
                sync_county(m["state_abbr"], m["county_name"])
            return jsonify({"ok": True, "synced": [dict(m) for m in matches]})
    return jsonify({"error": "provide ?state=&county= or ?zip="}), 400


@app.route("/api/ballot/<zip_code>")
def api_ballot(zip_code):
    """JSON API: everything on the ballot for a ZIP."""
    if not re.fullmatch(r"\d{5}", zip_code):
        return jsonify({"error": "invalid zip"}), 400
    matches = lookup_zip(zip_code)
    if not matches:
        return jsonify({"error": "unknown zip"}), 404
    out = {"zip": zip_code,
           "state": {"abbr": matches[0]["state_abbr"], "name": matches[0]["state_name"]},
           "counties": []}
    for m in matches:
        races, measures = ballot_for(m["state_abbr"], m["county_id"])
        out["counties"].append({
            "county": m["county_name"],
            "races": [{
                "office": r["office"]["title"], "level": r["office"]["level"],
                "district": r["office"]["district"],
                "candidates": [dict(c) for c in r["candidates"]],
            } for r in races],
            "measures": [dict(x) for x in measures],
        })
    return jsonify(out)


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

def bootstrap():
    fresh = not os.path.exists(DB_PATH)
    init_db()
    if fresh:
        db = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
        load_zip_data(db)
        db.close()
        print("Database initialized with ZIP/county data.")


bootstrap()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
