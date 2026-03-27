# Gmail Inbox Cleaner

An interactive web tool to bulk-unsubscribe and delete emails by sender directly in your Gmail inbox — sorted from most to least emails.

![Python](https://img.shields.io/badge/python-3.8+-blue) ![Flask](https://img.shields.io/badge/flask-latest-green) ![License](https://img.shields.io/badge/license-MIT-brightgreen)

## Features

- **Sorted sender list** — see who fills your inbox the most
- **One-click unsubscribe** — opens the unsubscribe link directly
- **Bulk delete** — moves all emails from a sender to Gmail Trash in seconds
- **Fast scan** — uses your local Google Takeout mbox file for instant results
- **Privacy-first** — runs entirely on your own machine

## How it works

1. Connects to the Gmail API and scans your inbox to build a sender index
2. Lets you move all emails from selected senders to Gmail Trash in one click
3. Emails in Trash are automatically deleted after 30 days (or you can empty it manually)

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Set up Gmail API credentials

1. Go to [Google Cloud Console](https://console.cloud.google.com)
2. Create a new project
3. Go to **APIs & Services** → search for **Gmail API** → click **Enable**
4. Go to **APIs & Services → OAuth consent screen**
   - Choose **External** → fill in app name and your email
   - Under **Test users**, add your Gmail address
5. Go to **Credentials → + Create Credentials → OAuth client ID**
   - Application type: **Desktop app**
   - Click **Download JSON**
6. Save the downloaded file as `credentials.json` in the same folder as `inbox_cleaner.py`

### 4. Run

```bash
python inbox_cleaner.py
```

The browser opens automatically. On first run, Google will ask you to grant access — click **Allow**.

## Usage

| Action | How |
|---|---|
| Load inbox | Click **Inbox laden** (first time takes a few minutes) |
| Search | Type in the search box |
| Unsubscribe | Click **Uitschrijven** (only shown if the email has an unsubscribe link) |
| Delete all from sender | Click **Verwijder alles** → confirm |
| Undo selection | Click **Ongedaan** before confirming |

## Privacy & Security

- `credentials.json` and `token.json` are listed in `.gitignore` — **never commit these**
- `Inbox.mbox` and `inbox_index.json` are also ignored — your email data stays local
- The app only requests the `gmail.modify` scope (read + move to trash, no send/delete permission)

## License

MIT
