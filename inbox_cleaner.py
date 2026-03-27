#!/usr/bin/env python3
"""Gmail Inbox Cleaner
Scans your inbox and deletes emails by sender directly via the Gmail API.
"""

import json
import os
import re
import sys
import time
import webbrowser
import threading
from email.header import decode_header
from flask import Flask, jsonify, request, render_template_string

# Gmail API
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ── Paden ───────────────────────────────────────────────────────────────────
BASE = os.path.dirname(os.path.abspath(__file__))
MBOX_PATH  = os.path.join(BASE, "Inbox.mbox")
INDEX_PATH = os.path.join(BASE, "inbox_index.json")
CREDS_PATH = os.path.join(BASE, "credentials.json")
TOKEN_PATH = os.path.join(BASE, "token.json")

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

# ── State ────────────────────────────────────────────────────────────────────
app = Flask(__name__)
gmail_service = None
scan_progress  = {"running": False, "percent": 0, "total": 0, "done": False, "error": None}
delete_progress = {"running": False, "percent": 0, "done": False, "error": None, "deleted": 0}


# ── Gmail authenticatie ──────────────────────────────────────────────────────
def get_service():
    global gmail_service
    if gmail_service:
        return gmail_service
    creds = None
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDS_PATH, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_PATH, "w") as fh:
            fh.write(creds.to_json())
    gmail_service = build("gmail", "v1", credentials=creds)
    return gmail_service


# ── mbox scan helpers ────────────────────────────────────────────────────────
def _decode(value):
    if not value:
        return ""
    try:
        parts = decode_header(value)
        out = []
        for part, cs in parts:
            if isinstance(part, bytes):
                out.append(part.decode(cs or "utf-8", errors="replace"))
            else:
                out.append(part)
        return " ".join(out).strip()
    except Exception:
        return str(value).strip()


def _email_name(header):
    if not header:
        return "", ""
    m = re.search(r"<([^>@\s]+@[^>]+)>", header)
    if m:
        addr = m.group(1).strip().lower()
        name = header[: m.start()].strip().strip('"').strip("'").strip()
        return addr, name or addr
    bare = re.search(r"[\w._%+\-]+@[\w.\-]+\.[a-z]{2,}", header, re.I)
    if bare:
        return bare.group(0).lower(), bare.group(0).lower()
    return header.strip().lower(), header.strip()


def _unsub_urls(val):
    if not val:
        return []
    urls = []
    for m in re.finditer(r"<([^>]+)>", val):
        u = m.group(1).strip()
        if u.startswith(("http://", "https://", "mailto:")):
            urls.append(u)
    return sorted(urls, key=lambda u: 0 if u.startswith("http") else 1)


def _process(headers, senders):
    addr, name = _email_name(_decode(headers.get("from", "")))
    if not addr:
        return
    urls = _unsub_urls(headers.get("list-unsubscribe", ""))
    if addr not in senders:
        senders[addr] = {"name": name, "count": 0, "unsubscribe_urls": []}
    senders[addr]["count"] += 1
    for u in urls:
        if u not in senders[addr]["unsubscribe_urls"]:
            senders[addr]["unsubscribe_urls"].append(u)


def scan_mbox():
    global scan_progress
    scan_progress = {"running": True, "percent": 0, "total": 0, "done": False, "error": None}
    try:
        size = os.path.getsize(MBOX_PATH)
        senders = {}
        cur_h = {}
        hname = hval = None
        in_h = False
        read = last_pct = 0

        with open(MBOX_PATH, "rb") as f:
            for raw in f:
                read += len(raw)
                try:
                    line = raw.decode("utf-8", errors="replace")
                except Exception:
                    continue

                if line.startswith("From ") and len(line) > 10 and " " in line[5:]:
                    if cur_h:
                        if hname and hval and hname.lower() not in cur_h:
                            cur_h[hname.lower()] = hval
                        _process(cur_h, senders)
                    cur_h = {}; in_h = True; hname = hval = None
                    pct = int(read / size * 100)
                    if pct != last_pct:
                        last_pct = pct
                        scan_progress["percent"] = pct
                        scan_progress["total"] = sum(s["count"] for s in senders.values())
                    continue

                if in_h:
                    s = line.rstrip("\r\n")
                    if s == "":
                        if hname and hval and hname.lower() not in cur_h:
                            cur_h[hname.lower()] = hval
                        in_h = False; hname = hval = None
                    elif s[0] in (" ", "\t") and hname:
                        hval = (hval or "") + " " + s.strip()
                    elif ":" in s:
                        if hname and hval and hname.lower() not in cur_h:
                            cur_h[hname.lower()] = hval
                        c = s.index(":")
                        hname = s[:c].strip(); hval = s[c+1:].strip()

        if cur_h:
            if hname and hval and hname.lower() not in cur_h:
                cur_h[hname.lower()] = hval
            _process(cur_h, senders)

        total = sum(s["count"] for s in senders.values())
        with open(INDEX_PATH, "w", encoding="utf-8") as f:
            json.dump({"senders": senders, "total": total}, f, ensure_ascii=False)
        scan_progress.update({"done": True, "running": False, "percent": 100, "total": total})
    except Exception as e:
        scan_progress.update({"error": str(e), "running": False})


# ── Gmail API verwijdering ───────────────────────────────────────────────────
def delete_senders_gmail(emails_to_delete):
    global delete_progress
    delete_progress = {"running": True, "percent": 0, "done": False, "error": None, "deleted": 0}
    try:
        svc = get_service()
        total_deleted = 0
        n = len(emails_to_delete)

        for i, addr in enumerate(emails_to_delete):
            # Verzamel alle message-IDs van deze afzender
            ids = []
            page_token = None
            while True:
                params = {"userId": "me", "q": f"from:{addr}", "maxResults": 500}
                if page_token:
                    params["pageToken"] = page_token
                resp = svc.users().messages().list(**params).execute()
                msgs = resp.get("messages", [])
                ids.extend(m["id"] for m in msgs)
                page_token = resp.get("nextPageToken")
                if not page_token:
                    break

            # Verplaats naar prullenbak in batches van 1000
            for j in range(0, len(ids), 1000):
                batch_ids = ids[j:j+1000]
                svc.users().messages().batchModify(
                    userId="me",
                    body={"ids": batch_ids, "addLabelIds": ["TRASH"], "removeLabelIds": ["INBOX"]}
                ).execute()
                total_deleted += len(batch_ids)
                time.sleep(0.1)  # rate limiting

            delete_progress["percent"] = int((i + 1) / n * 100)
            delete_progress["deleted"] = total_deleted

        # Update lokale index
        if os.path.exists(INDEX_PATH):
            with open(INDEX_PATH, "r", encoding="utf-8") as f:
                idx = json.load(f)
            for addr in emails_to_delete:
                idx["senders"].pop(addr, None)
            idx["total"] = sum(s["count"] for s in idx["senders"].values())
            with open(INDEX_PATH, "w", encoding="utf-8") as f:
                json.dump(idx, f, ensure_ascii=False)

        delete_progress.update({"done": True, "running": False, "percent": 100, "deleted": total_deleted})
    except Exception as e:
        delete_progress.update({"error": str(e), "running": False})


# ── HTML ─────────────────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Gmail Inbox Cleaner</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f0f2f5;color:#1f2937}
header{background:#1a73e8;color:#fff;padding:14px 24px;display:flex;align-items:center;gap:16px;box-shadow:0 2px 6px rgba(0,0,0,.2)}
header h1{font-size:18px;font-weight:600}
#stats{font-size:13px;opacity:.9;margin-left:auto}
.container{max-width:1100px;margin:20px auto;padding:0 16px}
.toolbar{display:flex;gap:10px;align-items:center;margin-bottom:14px;flex-wrap:wrap}
.toolbar input{flex:1;min-width:200px;padding:9px 14px;border:1px solid #d1d5db;border-radius:6px;font-size:14px;background:#fff}
.toolbar input:focus{outline:none;border-color:#1a73e8;box-shadow:0 0 0 3px rgba(26,115,232,.15)}
button{padding:9px 18px;border:none;border-radius:6px;cursor:pointer;font-size:13px;font-weight:500;transition:opacity .15s}
button:disabled{opacity:.4;cursor:not-allowed}
.btn-scan{background:#1a73e8;color:#fff}.btn-scan:hover:not(:disabled){background:#1557b0}
.btn-apply{background:#dc2626;color:#fff}.btn-apply:hover:not(:disabled){background:#b91c1c}
.badge{background:#dc2626;color:#fff;border-radius:10px;padding:1px 7px;font-size:11px;margin-left:6px}
.pbar-wrap{display:none;margin-bottom:12px}.pbar-wrap.vis{display:block}
.pbar-label{font-size:13px;color:#6b7280;margin-bottom:5px}
.pbar{height:6px;background:#e5e7eb;border-radius:3px;overflow:hidden}
.pbar-fill{height:100%;background:#1a73e8;border-radius:3px;transition:width .4s}
.card{background:#fff;border-radius:10px;box-shadow:0 1px 4px rgba(0,0,0,.08);overflow:hidden}
table{width:100%;border-collapse:collapse}
thead th{padding:10px 14px;text-align:left;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.6px;color:#6b7280;border-bottom:1px solid #e5e7eb;background:#f9fafb}
tbody tr{border-bottom:1px solid #f3f4f6;transition:background .1s}
tbody tr:last-child{border-bottom:none}
tbody tr:hover{background:#f0f7ff}
tbody tr.del{background:#fff5f5}
tbody tr.del .sname{text-decoration:line-through;color:#9ca3af}
td{padding:9px 14px;font-size:13px;vertical-align:middle}
.sname{font-weight:600;color:#111827}
.semail{font-size:11px;color:#9ca3af;margin-top:1px}
.bar-wrap{display:flex;align-items:center;gap:8px}
.bar-num{font-weight:700;color:#1a73e8;min-width:36px;font-size:14px}
.bar{height:8px;background:#bfdbfe;border-radius:4px;min-width:3px}
.bar-inner{height:100%;background:#1a73e8;border-radius:4px}
.acts{display:flex;gap:6px;flex-wrap:wrap}
.btn-unsub{padding:5px 11px;border:1px solid #1a73e8;color:#1a73e8;background:#fff;border-radius:4px;cursor:pointer;font-size:12px}
.btn-unsub:hover:not(:disabled){background:#eff6ff}
.btn-unsub:disabled{border-color:#d1d5db;color:#9ca3af;cursor:not-allowed}
.btn-del{padding:5px 11px;background:#fee2e2;color:#dc2626;border:none;border-radius:4px;cursor:pointer;font-size:12px;font-weight:500}
.btn-del:hover{background:#fecaca}
.btn-undo{padding:5px 11px;background:#dbeafe;color:#1a73e8;border:none;border-radius:4px;cursor:pointer;font-size:12px}
.overlay{position:fixed;inset:0;background:rgba(0,0,0,.45);display:none;align-items:center;justify-content:center;z-index:50}
.overlay.vis{display:flex}
.modal{background:#fff;border-radius:10px;padding:24px;max-width:440px;width:92%;box-shadow:0 10px 40px rgba(0,0,0,.2)}
.modal h2{font-size:16px;margin-bottom:10px}
.modal p{font-size:13px;color:#6b7280;margin-bottom:20px;line-height:1.6}
.modal-acts{display:flex;gap:10px;justify-content:flex-end}
.btn-cancel{background:#f3f4f6;color:#374151}.btn-cancel:hover{background:#e5e7eb}
.btn-confirm{background:#dc2626;color:#fff}.btn-confirm:hover{background:#b91c1c}
.info-box{background:#eff6ff;border:1px solid #bfdbfe;border-radius:8px;padding:16px;font-size:13px;line-height:1.7;margin-bottom:16px}
.info-box strong{color:#1e40af}
.empty{text-align:center;padding:60px 20px;color:#9ca3af;font-size:14px}
</style>
</head>
<body>
<header>
  <h1>&#x2709;&#xFE0F; Gmail Inbox Cleaner</h1>
  <span id="stats">Click "Load inbox" to get started</span>
</header>
<div class="container">
  <div class="toolbar">
    <input id="q" type="text" placeholder="Search by name or email address..." oninput="filter()">
    <button class="btn-scan" id="btn-scan" onclick="startScan()">Load inbox</button>
    <button class="btn-apply" id="btn-apply" onclick="askConfirm()" disabled>
      Move to trash<span class="badge" id="badge" style="display:none">0</span>
    </button>
  </div>
  <div class="pbar-wrap" id="pw">
    <div class="pbar-label" id="plabel">Loading...</div>
    <div class="pbar"><div class="pbar-fill" id="pfill" style="width:0%"></div></div>
  </div>
  <div class="card" id="card">
    <div class="empty">Click <strong>Load inbox</strong> to get started.<br>The first scan may take a few minutes.</div>
  </div>
</div>

<div class="overlay" id="modal">
  <div class="modal">
    <h2>Confirm deletion</h2>
    <p id="mtext"></p>
    <div class="modal-acts">
      <button class="btn-cancel" onclick="closeModal()">Cancel</button>
      <button class="btn-confirm" onclick="doDelete()">Move to trash</button>
    </div>
  </div>
</div>

<script>
let all=[], maxC=1, pending=new Set();
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
function setP(pct,lbl){document.getElementById('pw').classList.add('vis');document.getElementById('pfill').style.width=pct+'%';document.getElementById('plabel').textContent=lbl;}
function hideP(){document.getElementById('pw').classList.remove('vis');}

function startScan(){
  document.getElementById('btn-scan').disabled=true;
  document.getElementById('btn-scan').textContent='Loading...';
  setP(0,'Preparing...');
  fetch('/api/scan',{method:'POST'}).then(r=>r.json()).then(d=>{
    if(d.status==='cached'){loadSenders();}else{pollScan();}
  });
}

function pollScan(){
  fetch('/api/scan/progress').then(r=>r.json()).then(d=>{
    setP(d.percent,`Scanning... ${d.percent}% — ${d.total.toLocaleString()} messages`);
    if(d.done){loadSenders();}
    else if(d.error){setP(100,'Error: '+d.error);resetScanBtn();}
    else setTimeout(pollScan,800);
  });
}

function loadSenders(){
  fetch('/api/senders').then(r=>r.json()).then(d=>{
    all=d.senders; maxC=all.length?all[0].count:1;
    render(all);
    document.getElementById('stats').textContent=`${d.total.toLocaleString()} emails · ${d.sender_count.toLocaleString()} senders`;
    hideP(); resetScanBtn();
  });
}

function resetScanBtn(){
  document.getElementById('btn-scan').disabled=false;
  document.getElementById('btn-scan').textContent='Reload';
}

function render(list){
  if(!list.length){document.getElementById('card').innerHTML='<div class="empty">No results.</div>';return;}
  const rows=list.map(s=>{
    const w=Math.max(4,Math.round((s.count/maxC)*180));
    const isDel=pending.has(s.email);
    const hasUrl=s.unsubscribe_urls&&s.unsubscribe_urls.length>0;
    const url=hasUrl?s.unsubscribe_urls[0]:'';
    return `<tr class="${isDel?'del':''}">
      <td><div class="sname">${esc(s.name)}</div><div class="semail">${esc(s.email)}</div></td>
      <td><div class="bar-wrap"><span class="bar-num">${s.count.toLocaleString()}</span>
        <div class="bar" style="width:${w}px"><div class="bar-inner" style="width:100%"></div></div></div></td>
      <td><div class="acts">
        <button class="btn-unsub" ${hasUrl?`onclick="openUrl('${esc(url)}')"`:' disabled'} title="${hasUrl?esc(url):'No unsubscribe link found'}">
          ${hasUrl?'Unsubscribe':'No link'}</button>
        ${isDel
          ?`<button class="btn-undo" onclick="unmark('${esc(s.email)}')">&#8617; Undo</button>`
          :`<button class="btn-del" onclick="mark('${esc(s.email)}')">&#128465; Delete all</button>`}
      </div></td>
    </tr>`;
  }).join('');
  document.getElementById('card').innerHTML=`<table>
    <thead><tr><th>Sender</th><th>Emails</th><th>Actions</th></tr></thead>
    <tbody>${rows}</tbody></table>`;
}

function filter(){
  const q=document.getElementById('q').value.toLowerCase();
  render(q?all.filter(s=>s.name.toLowerCase().includes(q)||s.email.toLowerCase().includes(q)):all);
}

function mark(e){pending.add(e);badge();filter();}
function unmark(e){pending.delete(e);badge();filter();}
function badge(){
  const n=pending.size,b=document.getElementById('badge');
  document.getElementById('btn-apply').disabled=!n;
  b.style.display=n?'inline':'none';b.textContent=n;
}

function openUrl(url){
  if(url.startsWith('mailto:'))window.location.href=url;
  else window.open(url,'_blank','noopener');
}

function askConfirm(){
  const n=pending.size;
  document.getElementById('mtext').innerHTML=
    `You are about to move all emails from <strong>${n} sender${n===1?'':'s'}</strong> to Gmail Trash.<br><br>
    Emails stay in Trash for 30 days and can be restored. After 30 days they are permanently deleted.`;
  document.getElementById('modal').classList.add('vis');
}
function closeModal(){document.getElementById('modal').classList.remove('vis');}

function doDelete(){
  closeModal();
  document.getElementById('btn-apply').disabled=true;
  document.getElementById('btn-scan').disabled=true;
  setP(0,'Connecting to Gmail...');
  fetch('/api/delete',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({emails:[...pending]})}).then(r=>r.json()).then(d=>{
      if(d.error){setP(0,'Error: '+d.error);document.getElementById('btn-scan').disabled=false;return;}
      pollDelete();
    });
}

function pollDelete(){
  fetch('/api/delete/progress').then(r=>r.json()).then(d=>{
    setP(d.percent,`Deleting via Gmail API... ${d.percent}% — ${d.deleted.toLocaleString()} emails moved`);
    if(d.done){
      pending.clear();badge();
      setP(100,`Done! ${d.deleted.toLocaleString()} emails moved to trash.`);
      document.getElementById('btn-scan').disabled=false;
      loadSenders();
    }else if(d.error){
      setP(100,'Error: '+d.error);
      document.getElementById('btn-scan').disabled=false;
    }else setTimeout(pollDelete,800);
  });
}

fetch('/api/status').then(r=>r.json()).then(d=>{if(d.has_index)loadSenders();});
</script>
</body>
</html>"""


# ── Flask routes ─────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template_string(HTML)


@app.route("/api/status")
def api_status():
    return jsonify({"has_index": os.path.exists(INDEX_PATH)})


@app.route("/api/scan", methods=["POST"])
def api_scan():
    if scan_progress["running"]:
        return jsonify({"status": "already_running"})
    if os.path.exists(INDEX_PATH):
        return jsonify({"status": "cached"})
    threading.Thread(target=scan_mbox, daemon=True).start()
    return jsonify({"status": "started"})


@app.route("/api/scan/progress")
def api_scan_progress():
    return jsonify(scan_progress)


@app.route("/api/senders")
def api_senders():
    if not os.path.exists(INDEX_PATH):
        return jsonify({"error": "not found"}), 404
    with open(INDEX_PATH, "r", encoding="utf-8") as f:
        idx = json.load(f)
    senders = sorted(
        [{"email": k, "name": v["name"], "count": v["count"], "unsubscribe_urls": v["unsubscribe_urls"]}
         for k, v in idx["senders"].items()],
        key=lambda x: x["count"], reverse=True
    )
    return jsonify({"senders": senders, "total": idx["total"], "sender_count": len(senders)})


@app.route("/api/delete", methods=["POST"])
def api_delete():
    if delete_progress["running"]:
        return jsonify({"status": "already_running"})
    if not os.path.exists(CREDS_PATH):
        return jsonify({"error": "credentials.json not found — see setup instructions in the terminal"}), 400
    emails = request.json.get("emails", [])
    if not emails:
        return jsonify({"status": "no_emails"})
    threading.Thread(target=delete_senders_gmail, args=(emails,), daemon=True).start()
    return jsonify({"status": "started"})


@app.route("/api/delete/progress")
def api_delete_progress():
    return jsonify(delete_progress)


# ── Startup ──────────────────────────────────────────────────────────────────
def check_credentials():
    """Check if credentials.json exists and run OAuth flow if needed."""
    if not os.path.exists(CREDS_PATH):
        print("\n" + "="*60)
        print("  SETUP REQUIRED: Gmail API access")
        print("="*60)
        print("""
Step 1 - Go to: https://console.cloud.google.com/
Step 2 - Create a new project (or select existing).
Step 3 - Search for 'Gmail API' and click 'Enable'.
Step 4 - Go to 'APIs & Services' -> 'Credentials'.
Step 5 - Click '+ Create Credentials' -> 'OAuth client ID'.
         Choose type: 'Desktop app'.
Step 6 - Click 'Download JSON' and save the file as:

         """ + CREDS_PATH + """

Step 7 - Restart this script.

Tip: under 'OAuth consent screen', choose 'External' and add
     your own Gmail address as a test user.
""")
        print("="*60 + "\n")
        return False

    try:
        print("\nChecking Gmail authentication...")
        get_service()
        print("OK Gmail connection successful!\n")
        return True
    except Exception as e:
        print(f"ERROR Gmail authentication failed: {e}")
        print("  Delete token.json and try again.\n")
        return False


if __name__ == "__main__":
    print("\nGmail Inbox Cleaner")
    check_credentials()

    def open_browser():
        time.sleep(1.5)
        webbrowser.open("http://localhost:5000")

    print("Web interface running at: http://localhost:5000")
    threading.Thread(target=open_browser, daemon=True).start()
    app.run(debug=False, port=5000, threaded=True)
