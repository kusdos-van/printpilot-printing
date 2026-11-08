"""PrintPilot Messenger printing-order intake. Set secrets in Render environment variables, never in Git."""
import os, re, io, csv, json, time, hmac, hashlib, sqlite3, secrets
from pathlib import Path
from urllib.parse import urlsplit
from functools import wraps
from datetime import datetime, timezone
from flask import Flask, request, jsonify, Response, send_file, abort, redirect, render_template_string
import requests

try:
    import fitz
except ImportError:
    fitz = None
try:
    from PIL import Image
except ImportError:
    Image = None

PAGE_ID = os.getenv("PAGE_ID", "61595456493475")
PAGE_TOKEN = os.getenv("PAGE_ACCESS_TOKEN", "")
META_SECRET = os.getenv("META_APP_SECRET", "")
VERIFY_TOKEN = os.getenv("META_VERIFY_TOKEN", "")
ADMIN_USER = os.getenv("ADMIN_USERNAME", "owner")
ADMIN_PASS = os.getenv("ADMIN_PASSWORD", "")
GRAPH_VERSION = os.getenv("GRAPH_API_VERSION", "v24.0")
DATA = Path(os.getenv("DATA_DIR", "/tmp/printpilot"))
DATA.mkdir(parents=True, exist_ok=True)
(DATE_FILES := DATA / "files").mkdir(exist_ok=True)
DB = DATA / "printpilot.sqlite3"
MAX_BYTES = 15 * 1024 * 1024
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_BYTES + 1024 * 1024

PRICES = {"bw": float(os.getenv("PRICE_BW", "3")), "color": float(os.getenv("PRICE_COLOR", "12"))}
SIZES = ["A4", "Letter", "Legal", "A5"]
SIDES = ["Single-sided", "Double-sided"]
MARGINS = ["Normal (1 inch)", "Narrow (0.5 inch)", "Keep original"]
DELIVERY = ["Pickup", "Delivery inquiry"]

def db():
    con = sqlite3.connect(str(DB), timeout=10)
    con.row_factory = sqlite3.Row
    return con

with db() as conn:
    conn.execute("""CREATE TABLE IF NOT EXISTS sessions(
        user_id TEXT PRIMARY KEY, stage TEXT NOT NULL, fields TEXT NOT NULL,
        touched REAL NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS orders(
        id TEXT PRIMARY KEY, user_id TEXT NOT NULL, created TEXT NOT NULL,
        data TEXT NOT NULL, file_path TEXT, file_name TEXT,
        file_report TEXT, status TEXT NOT NULL)""")

def save_session(uid, stage, fields):
    with db() as c:
        c.execute("INSERT INTO sessions VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET stage=excluded.stage, fields=excluded.fields,touched=excluded.touched",
                  (uid, stage, json.dumps(fields), time.time()))

def get_session(uid):
    with db() as c:
        row = c.execute("SELECT * FROM sessions WHERE user_id=?", (uid,)).fetchone()
    if not row or time.time() - row["touched"] > 86400:
        return "start", {}
    return row["stage"], json.loads(row["fields"])

def inspect_document(blob, filename):
    name = (filename or "attachment")[:100]
    fmt = "unknown"
    details = []
    pages = None
    if blob.startswith(b"%PDF-"):
        fmt = "PDF"
        if not fitz:
            details.append("PDF scanner unavailable: manual review needed")
        else:
            try:
                with fitz.open(stream=blob, filetype="pdf") as doc:
                    pages = len(doc)
                    if pages > 300: raise ValueError("Over 300 pages; manual review required")
                    sizes = {(round(p.rect.width / 72, 2), round(p.rect.height / 72, 2)) for p in doc}
                    details.append("Sizes (inches): " + ", ".join(f"{w} × {h}" for w, h in sorted(sizes)))
                    details.append("Contains password protection: " + ("Yes" if doc.needs_pass else "No"))
            except Exception as exc:
                details.append("Unable to inspect PDF: " + type(exc).__name__)
    elif blob[:3] == b"\xff\xd8\xff" or blob[:8] == b"\x89PNG\r\n\x1a\n":
        fmt = "Image"
        if Image:
            try:
                with Image.open(io.BytesIO(blob)) as im:
                    im.verify()
                with Image.open(io.BytesIO(blob)) as im:
                    details.append(f"Resolution: {im.width} × {im.height} pixels")
                pages = 1
            except Exception:
                raise ValueError("Invalid image file")
    elif blob.startswith(b"PK\x03\x04"):
        fmt = "Office or ZIP"
        details.append("Office files must be manually reviewed for page count and formatting")
    else:
        raise ValueError("Only PDF, PNG, JPG and Office files are supported")
    return {"format": fmt, "pages": pages, "details": details, "review_required": pages is None}

def valid_url(u):
    parsed = urlsplit(u)
    host = (parsed.hostname or "").lower().rstrip(".")
    domains = ("fbcdn.net", "fbsbx.com", "facebook.com")
    return parsed.scheme == "https" and parsed.port in (None, 443) and parsed.username is None and any(host == d or host.endswith("." + d) for d in domains)

def download_attachment(url):
    if not valid_url(url):
        raise ValueError("Unsupported attachment location")
    # Requests disables automatic redirects so every hop is checked.
    for _ in range(5):
        resp = requests.get(url, timeout=20, stream=True, allow_redirects=False)
        if resp.status_code in (301,302,303,307,308):
            url = resp.headers.get("Location", "")
            if not valid_url(url): raise ValueError("Unexpected redirect location")
            continue
        resp.raise_for_status()
        buf = io.BytesIO()
        for block in resp.iter_content(65536):
            buf.write(block)
            if buf.tell() > MAX_BYTES: raise ValueError("File exceeds 15MB")
        return buf.getvalue()
    raise ValueError("Too many file redirects")

def messenger_send(uid, text, choices=None):
    if not PAGE_TOKEN:
        return False
    msg = {"text": text[:1900]}
    if choices:
        msg["quick_replies"] = [{"content_type":"text", "title":opt[:20], "payload":opt[:100]} for opt in choices[:13]]
    body = {"recipient":{"id":uid}, "messaging_type":"RESPONSE", "message":msg}
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{PAGE_ID}/messages"
    r = requests.post(url, headers={"Authorization":"Bearer " + PAGE_TOKEN}, json=body, timeout=15)
    r.raise_for_status()
    return True

def estimate(fields):
    report = fields.get("report") or {}
    pages = report.get("pages")
    if not pages:
        return None
    copies = int(fields.get("copies", 1))
    factor = 0.9 if fields.get("sides") == "Double-sided" else 1
    return round(pages * copies * PRICES[fields.get("color", "bw")] * factor, 2)

def reply(uid, message, choices=None):
    messenger_send(uid, message, choices)

def handle_text(uid, raw, attachment=None):
    stage, state = get_session(uid)
    value = (raw or "").strip()
    if value.lower() in ("restart", "reset", "cancel", "menu"):
        stage, state = "start", {}
    if stage == "start":
        save_session(uid, "size", state)
        return reply(uid, "Welcome to PrintPilot! Let's set up your printing order. What paper size would you like?", SIZES)
    if stage == "size":
        if value not in SIZES: return reply(uid, "Please choose a paper size:", SIZES)
        state["size"] = value
        save_session(uid, "color", state)
        return reply(uid, "Print in color or black and white?", ["B&W", "Color"])
    if stage == "color":
        if value not in ("B&W", "Color"): return reply(uid, "Select B&W or Color:", ["B&W", "Color"])
        state["color"] = "bw" if value == "B&W" else "color"
        save_session(uid, "copies", state)
        return reply(uid, "How many copies? Reply with a number from 1 to 100.")
    if stage == "copies":
        if not value.isdigit() or not 1 <= int(value) <= 100:
            return reply(uid, "Reply with a whole number from 1 to 100.")
        state["copies"] = int(value)
        save_session(uid, "margins", state)
        return reply(uid, "Choose your margin setting:", MARGINS)
    if stage == "margins":
        if value not in MARGINS: return reply(uid, "Please select a margin:", MARGINS)
        state["margins"] = value
        save_session(uid, "sides", state)
        return reply(uid, "Print on one or both sides?", SIDES)
    if stage == "sides":
        if value not in SIDES: return reply(uid, "Choose a printing side:", SIDES)
        state["sides"] = value
        save_session(uid, "file", state)
        return reply(uid, "Please attach your PDF, JPG, PNG, or Word document here (maximum 15 MB). Type RESTART to begin again.")
    if stage == "file":
        if attachment is None: return reply(uid, "Please send the file as a Messenger attachment.")
        try:
            blob = download_attachment(attachment.get("url", ""))
            filename = attachment.get("filename") or f"document-{int(time.time())}"
            report = inspect_document(blob, filename)
            saved = secrets.token_hex(16) + (".pdf" if report["format"] == "PDF" else ".png" if report["format"] == "Image" else ".office")
            (DATE_FILES / saved).write_bytes(blob)
            state.update({"saved": saved, "filename": filename, "report": report})
        except Exception as exc:
            return reply(uid, "I could not read this document. Please send a supported, unprotected file below 15MB. " + str(exc)[:90])
        save_session(uid, "fulfillment", state)
        scan = f"{report['format']}, {report['pages'] or 'unknown'} page(s)"
        return reply(uid, f"Document received and inspected: {scan}. Do you prefer pickup or delivery?", DELIVERY)
    if stage == "fulfillment":
        if value not in DELIVERY: return reply(uid, "Select pickup or delivery:", DELIVERY)
        state["fulfillment"] = value
        save_session(uid, "confirm", state)
        quote = estimate(state)
        amount = f"Estimated printing cost: PHP {quote:.2f} (before any delivery fee)." if quote is not None else "Price requires manual review."
        summary = f"Paper: {state['size']}; Color: {state['color'].upper()}; Copies: {state['copies']}; Margins: {state['margins']}; Sides: {state['sides']}; {value}. {amount}"
        return reply(uid, summary + "\nSubmit this order?", ["Submit order", "Restart"])
    if stage == "confirm":
        if value == "Restart":
            save_session(uid, "start", {})
            return handle_text(uid, "start")
        if value != "Submit order": return reply(uid, "Please confirm:", ["Submit order", "Restart"])
        oid = "PP-" + datetime.now(timezone.utc).strftime("%y%m%d") + "-" + secrets.token_hex(3).upper()
        with db() as c:
            c.execute("INSERT INTO orders VALUES(?,?,?,?,?,?,?,?)",
                (oid, uid, datetime.now(timezone.utc).isoformat(), json.dumps({k:v for k,v in state.items() if k not in ("saved", "report")}),
                 state.get("saved"), state.get("filename"), json.dumps(state.get("report")), "New"))
        save_session(uid, "start", {})
        return reply(uid, f"Order {oid} received! Our printing staff will check the layout and confirm the final price. Please keep this order number. Type MENU to start another order.")
    save_session(uid, "start", {})
    return handle_text(uid, "menu")

@app.get("/health")
def health():
    return jsonify(status="ok", configured=bool(PAGE_TOKEN and META_SECRET and VERIFY_TOKEN and ADMIN_PASS), page_id=PAGE_ID)

@app.get("/webhook")
def webhook_verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token", "")
    challenge = request.args.get("hub.challenge", "")
    if mode == "subscribe" and VERIFY_TOKEN and hmac.compare_digest(token, VERIFY_TOKEN):
        return Response(challenge, mimetype="text/plain")
    abort(403)

@app.post("/webhook")
def webhook_receive():
    if not (META_SECRET and PAGE_TOKEN and VERIFY_TOKEN): abort(503)
    signature = request.headers.get("X-Hub-Signature-256", "")
    expected = "sha256=" + hmac.new(META_SECRET.encode(), request.get_data(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected): abort(403)
    data = request.get_json(silent=True) or {}
    if data.get("object") != "page": abort(400)
    for entry in data.get("entry", []):
        if str(entry.get("id")) != PAGE_ID: continue
        for event in entry.get("messaging", []):
            sender = str(event.get("sender", {}).get("id", ""))
            msg = event.get("message", {})
            if not sender or not msg or msg.get("is_echo"): continue
            attachment = None
            for att in msg.get("attachments", []):
                if att.get("type") == "file" or att.get("type") == "image":
                    attachment = {"url": att.get("payload", {}).get("url", ""), "filename": att.get("name", "")}
                    break
            value = msg.get("quick_reply", {}).get("payload") or msg.get("text", "")
            try:
                handle_text(sender, value, attachment)
            except Exception:
                app.logger.exception("Could not process Messenger event")
    return jsonify(ok=True)

def authorized():
    if not ADMIN_PASS or len(ADMIN_PASS) < 12: return False
    header = request.authorization
    return bool(header and hmac.compare_digest(header.username or "", ADMIN_USER) and hmac.compare_digest(header.password or "", ADMIN_PASS))

def require_admin(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not authorized():
            return Response("Admin sign-in required", 401, {"WWW-Authenticate":'Basic realm="PrintPilot Admin"'})
        return fn(*a, **kw)
    return wrapper

def csrf():
    return hmac.new(ADMIN_PASS.encode(), b"printpilot-admin-action", hashlib.sha256).hexdigest()

HTML = """<!doctype html><html lang="en"><meta name="viewport" content="width=device-width, initial-scale=1"><title>PrintPilot Orders</title>
<style>body{font:16px system-ui;background:#f2f5fa;margin:0;color:#1a2538}header{background:#164fbb;color:white;padding:22px 5%}
main{max-width:1100px;margin:24px auto;padding:0 14px}article{background:white;padding:20px;border-radius:14px;box-shadow:0 2px 12px #0001;margin-bottom:16px}
h1{margin:0 0 6px}p{line-height:1.5}.muted{color:#596575;font-size:13px}.row{display:flex;flex-wrap:wrap;gap:12px;align-items:center}
button,a.btn{border:0;padding:11px 13px;border-radius:9px;background:#164fbb;color:#fff;text-decoration:none;cursor:pointer}
select{padding:10px;border-radius:8px;border:1px solid #bec7d4}
code{word-break:break-word}a{color:#164fbb}</style>
<header><h1>PrintPilot</h1><div>Facebook printing orders · Page {{page}}</div></header>
<main><article><b>Deployment status:</b> {{mode}}<p class="muted">Quotes are estimates; review each file before printing. This dashboard requires HTTPS and an admin password.</p><a href="/admin/export">Export orders CSV</a></article>
{% for o in orders %}
<article><div class="row"><h3 style="margin:0">{{o['id']}}</h3><b>{{o['status']}}</b></div><p class="muted">{{o['created']}}</p>
<p>{{o['data']}}</p><p><b>File:</b> {{o['file_name'] or 'None'}} {% if o['file_path'] %}<a href="/admin/file/{{o['id']}}">Download</a>{% endif %}</p>
<p><b>Inspection:</b> {{o['file_report']}}</p>
<form action="/admin/status/{{o['id']}}" method="post" class="row"><input type="hidden" name="csrf" value="{{csrf}}">
<select name="status">{% for s in statuses %}<option {% if s == o['status'] %}selected{% endif %}>{{s}}</option>{% endfor %}</select><button type="submit">Update status</button></form>
</article>
{% else %}<article>No printing orders yet.</article>{% endfor %}
</main></html>"""

@app.get("/")
def home():
    return redirect("/admin")

@app.get("/admin")
@require_admin
def dashboard():
    with db() as c:
        orders = [dict(row) for row in c.execute("SELECT * FROM orders ORDER BY created DESC LIMIT 200")]
    return render_template_string(HTML, orders=orders, statuses=["New","Review","Printing","Ready","Completed","Cancelled"], page=PAGE_ID, csrf=csrf(), mode="Messenger enabled" if PAGE_TOKEN and META_SECRET else "Awaiting Meta authorization")

@app.post("/admin/status/<order_id>")
@require_admin
def status_change(order_id):
    if not hmac.compare_digest(request.form.get("csrf", ""), csrf()): abort(403)
    status = request.form.get("status", "")
    if status not in ("New","Review","Printing","Ready","Completed","Cancelled"): abort(400)
    with db() as c:
        c.execute("UPDATE orders SET status=? WHERE id=?", (status, order_id))
    return redirect("/admin")

@app.get("/admin/file/<order_id>")
@require_admin
def order_file(order_id):
    with db() as c:
        row = c.execute("SELECT file_path,file_name FROM orders WHERE id=?", (order_id,)).fetchone()
    if not row or not row["file_path"]: abort(404)
    path = DATE_FILES / Path(row["file_path"]).name
    if not path.is_file(): abort(404)
    return send_file(path, as_attachment=True, download_name=Path(row["file_name"] or "document").name)

@app.get("/admin/export")
@require_admin
def export_orders():
    with db() as c:
        records = [dict(x) for x in c.execute("SELECT id,created,status,data,file_name,file_report FROM orders ORDER BY created DESC")]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=["id","created","status","data","file_name","file_report"])
    writer.writeheader();writer.writerows(records)
    return Response(buf.getvalue(), mimetype="text/csv", headers={"Content-Disposition":"attachment; filename=printpilot_orders.csv"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
