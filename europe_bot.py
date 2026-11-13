"""Bot de candidatures - Europe (decouverte automatique).
1) Decouvre des hotels/restaurants avec e-mail dans OpenStreetMap (Overpass API, donnees ouvertes).
2) Envoie le CV + une lettre courte (template_eu.txt) a quelques etablissements par jour.
MODE=dry : aucune e-mail envoyee (simulation). MODE=real : envoi reel.
"""
import json, os, random, re, smtplib, sqlite3, ssl, sys, time, datetime, urllib.parse, urllib.request
from email.message import EmailMessage
from pathlib import Path

BASE = Path(__file__).parent
MODE = os.environ.get("MODE", "dry").strip().lower()
MAX_PER_DAY = int(os.environ.get("MAX_PER_DAY", "8"))
COUNTRIES = [c.strip().upper() for c in os.environ.get("COUNTRIES", "PT,ES,GR,HR,MT,CY").split(",") if c.strip()]
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
APPLICANT_NAME = os.environ.get("APPLICANT_NAME", "Applicant")
REPLY_TO = os.environ.get("REPLY_TO", "") or GMAIL_ADDRESS
REPORT_TO = os.environ.get("REPORT_TO", "") or REPLY_TO
CV_FILE = BASE / "cv.pdf"
DB_FILE = BASE / "bot.db"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
BAD_EMAIL_RE = re.compile(r"(no-?reply|donotreply|privacy|dpo|gdpr|abuse|postmaster|webmaster)@", re.I)
COUNTRY_NAMES = {"PT": "Portugal", "ES": "Spain", "IT": "Italy", "GR": "Greece", "HR": "Croatia",
                 "MT": "Malta", "CY": "Cyprus", "FR": "France", "DE": "Germany", "NL": "the Netherlands",
                 "BE": "Belgium", "AT": "Austria", "IE": "Ireland", "SI": "Slovenia", "ME": "Montenegro"}


class SafeDict(dict):
    def __missing__(self, key):
        return ""


def db():
    con = sqlite3.connect(DB_FILE)
    con.execute("CREATE TABLE IF NOT EXISTS candidates (email TEXT PRIMARY KEY, name TEXT, country TEXT, city TEXT, kind TEXT, added_at TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS sent (email TEXT PRIMARY KEY, name TEXT, country TEXT, sent_at TEXT)")
    return con


def load_exclude():
    f = BASE / "exclude.txt"
    if not f.exists():
        return set()
    return {l.strip().lower() for l in f.read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")}


def overpass_query(country):
    return f"""[out:json][timeout:120];
area["ISO3166-1"="{country}"][admin_level=2]->.a;
(
  nwr["tourism"~"^(hotel|resort)$"]["email"](area.a);
  nwr["amenity"~"^(restaurant|cafe|bar)$"]["email"](area.a);
);
out tags 3000;"""


def fetch_overpass(country):
    data = urllib.parse.urlencode({"data": overpass_query(country)}).encode()
    req = urllib.request.Request(OVERPASS_URL, data=data, headers={
        "User-Agent": f"job-bot-eu/1.0 (personal job search; contact {REPLY_TO})"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.load(r)


def discover(con, country, fetch=fetch_overpass):
    payload = fetch(country)
    added = 0
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    for el in payload.get("elements", []):
        t = el.get("tags", {})
        name = (t.get("name") or "").strip()
        raw = (t.get("email") or t.get("contact:email") or "").split(";")[0].strip().lower()
        if not name or not EMAIL_RE.match(raw) or BAD_EMAIL_RE.search(raw):
            continue
        kind = t.get("tourism") or t.get("amenity") or ""
        city = (t.get("addr:city") or "").strip()
        cur = con.execute("INSERT OR IGNORE INTO candidates VALUES (?,?,?,?,?,?)", (raw, name, country, city, kind, now))
        added += cur.rowcount
    con.commit()
    return added


BAD_KINDS = {"hostel", "guest_house", "apartment", "pub"}
FREE_MAIL = {"gmail.com", "hotmail.com", "hotmail.pt", "outlook.com", "yahoo.com", "yahoo.es", "live.com",
             "sapo.pt", "icloud.com", "libero.it", "mail.com", "gmx.com"}


def is_free(email):
    return email.rsplit("@", 1)[-1] in FREE_MAIL


def pick_candidates(con, exclude, n):
    """Round-robin entre pays, au hasard dans chaque pays."""
    pools = {}
    for c in COUNTRIES:
        rows = con.execute("""SELECT email,name,country,city,kind FROM candidates
                              WHERE country=? AND email NOT IN (SELECT email FROM sent)""", (c,)).fetchall()
        rows = [r for r in rows if r[0] not in exclude and r[4] not in BAD_KINDS]
        random.shuffle(rows)
        rows.sort(key=lambda r: is_free(r[0]), reverse=True)  # adresses pro en dernier => pop() les prend d'abord
        pools[c] = rows
    chosen = []
    while len(chosen) < n and any(pools.values()):
        for c in COUNTRIES:
            if pools[c] and len(chosen) < n:
                chosen.append(pools[c].pop())
    return chosen


def load_template():
    text = (BASE / "template_eu.txt").read_text(encoding="utf-8")
    first, _, rest = text.partition("\n")
    if not first.lower().startswith("subject:"):
        sys.exit("template_eu.txt doit commencer par 'Subject: ...'")
    return first.split(":", 1)[1].strip(), rest.strip() + "\n"


def build_message(cand, subject_t, body_t):
    email, name, country, city, kind = cand
    data = SafeDict(name=name, country_name=COUNTRY_NAMES.get(country, country),
                    city_phrase=f" in {city}" if city else "")
    msg = EmailMessage()
    msg["Subject"] = subject_t.format_map(data)
    msg["From"] = f"{APPLICANT_NAME} <{GMAIL_ADDRESS}>"
    msg["To"] = email
    if REPLY_TO:
        msg["Reply-To"] = REPLY_TO
    msg.set_content(body_t.format_map(data))
    msg.add_attachment(CV_FILE.read_bytes(), maintype="application", subtype="pdf",
                       filename=f"{APPLICANT_NAME.replace(' ', '_')}_CV.pdf")
    return msg


def smtp_send(msg):
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as s:
        s.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        s.send_message(msg)


def send_report(lines):
    body = "\n".join(lines)
    print(body)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("```\n" + body + "\n```\n")
    if MODE == "real" and REPORT_TO:
        m = EmailMessage()
        m["Subject"] = f"Rapport Europe du {datetime.date.today()} - mode: {MODE.upper()}"
        m["From"] = GMAIL_ADDRESS
        m["To"] = REPORT_TO
        m.set_content(body)
        try:
            smtp_send(m)
        except Exception as e:
            print("Rapport non envoye:", e)


def main(fetch=fetch_overpass, sleep=time.sleep):
    if not CV_FILE.exists():
        sys.exit("cv.pdf introuvable")
    subject_t, body_t = load_template()
    con = db()
    exclude = load_exclude()
    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    log, errors = [], []

    # 1) decouverte : on remplit le reservoir si un pays en manque (max 2 pays par jour)
    refilled = 0
    for c in COUNTRIES:
        if refilled >= 2:
            break
        unsent = con.execute("SELECT COUNT(*) FROM candidates WHERE country=? AND email NOT IN (SELECT email FROM sent)", (c,)).fetchone()[0]
        if unsent >= MAX_PER_DAY * 5:
            continue
        try:
            added = discover(con, c, fetch)
            log.append(f"Decouverte {c}: +{added} etablissements avec e-mail")
        except Exception as e:
            errors.append(f"Decouverte {c} impossible: {e}")
        refilled += 1
        sleep(10)

    # 2) envoi
    already_today = con.execute("SELECT COUNT(*) FROM sent WHERE sent_at LIKE ?", (today + "%",)).fetchone()[0]
    budget = max(0, MAX_PER_DAY - already_today)
    sent, failures = [], 0
    for cand in pick_candidates(con, exclude, budget):
        email, name, country, city, kind = cand
        msg = build_message(cand, subject_t, body_t)
        if MODE != "real":
            sent.append(f"[SIMULATION] {name} ({country}) <{email}>")
            continue
        try:
            smtp_send(msg)
            con.execute("INSERT INTO sent VALUES (?,?,?,?)", (email, name, country, datetime.datetime.now(datetime.timezone.utc).isoformat()))
            con.commit()
            sent.append(f"{name} ({country}) <{email}>")
            failures = 0
            sleep(random.randint(30, 60))
        except Exception as e:
            errors.append(f"{name}: {e}")
            failures += 1
            if failures >= 3:
                errors.append("3 echecs de suite: arret.")
                break

    pool = {c: con.execute("SELECT COUNT(*) FROM candidates WHERE country=? AND email NOT IN (SELECT email FROM sent)", (c,)).fetchone()[0] for c in COUNTRIES}
    cfg = (f"Config: GMAIL_ADDRESS={'OK' if GMAIL_ADDRESS else 'MANQUANT'} | "
           f"GMAIL_APP_PASSWORD={'OK' if GMAIL_APP_PASSWORD else 'MANQUANT'} | "
           f"APPLICANT_NAME={'OK' if os.environ.get('APPLICANT_NAME') else 'MANQUANT'}")
    lines = [f"Rapport du {today} - mode: {MODE.upper()}", cfg, "",
             f"Envoyes: {len(sent)} | erreurs: {len(errors)}", "",
             "Reservoir restant par pays: " + ", ".join(f"{c}={n}" for c, n in pool.items()), ""]
    if log:
        lines += log + [""]
    if sent:
        lines += ["Envoyes:"] + [f"- {x}" for x in sent] + [""]
    if errors:
        lines += ["Erreurs:"] + [f"- {x}" for x in errors] + [""]
    if not sent and not errors:
        lines.append("Rien a envoyer aujourd'hui.")
    send_report(lines)


if __name__ == "__main__":
    main()
