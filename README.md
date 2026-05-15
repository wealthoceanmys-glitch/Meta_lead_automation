# Meta Leads to Google Sheets Backend

FastAPI backend for:

Meta Lead Form -> Meta Webhook -> FastAPI -> Google Sheets

WhatsApp API can be added later after your WhatsApp phone number is fully registered.

## 1. Google Sheet

Create a Google Sheet and a tab named `Leads`.

Add this header row:

```text
Received At | Lead ID | Created Time | Name | Phone | Email | City | Course | Form ID | Page ID | Ad ID | Campaign ID | Raw Data
```

Copy the Sheet ID from the URL:

```text
https://docs.google.com/spreadsheets/d/SHEET_ID_HERE/edit
```

## 2. Google Service Account

1. Create a Google Cloud project.
2. Enable Google Sheets API.
3. Create Service Account.
4. Create JSON key.
5. Download it as `service_account.json`.
6. Share the Google Sheet with the service account email as Editor.

## 3. Local run

Copy `.env.example` to `.env` and fill values.

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Open:

```text
http://localhost:8000/
```

## 4. Render deployment

### Option A: Manual deploy

1. Push this folder to GitHub.
2. Render -> New -> Web Service.
3. Connect repo.
4. Build command:

```bash
pip install -r requirements.txt
```

5. Start command:

```bash
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

6. Add environment variables:

```env
META_VERIFY_TOKEN=woi_leads_verify_123
META_GRAPH_VERSION=v22.0
META_PAGE_ACCESS_TOKEN=your_page_access_token
GOOGLE_SHEET_ID=your_sheet_id
GOOGLE_SERVICE_ACCOUNT_JSON_BASE64=your_base64_service_account_json
```

### How to create GOOGLE_SERVICE_ACCOUNT_JSON_BASE64

On your computer, in the folder where `service_account.json` exists:

Windows PowerShell:

```powershell
[Convert]::ToBase64String([IO.File]::ReadAllBytes("service_account.json")) | Set-Clipboard
```

Mac/Linux:

```bash
base64 -w 0 service_account.json
```

Paste the output into Render env var `GOOGLE_SERVICE_ACCOUNT_JSON_BASE64`.

## 5. Meta webhook setup

After Render deploys, your callback URL will be:

```text
https://YOUR-RENDER-APP.onrender.com/webhook/meta-leads
```

Verify token should match:

```text
woi_leads_verify_123
```

In Meta Developer App:

```text
Webhooks -> Page -> Subscribe to leadgen
```

Then subscribe the Facebook Page to the app.

## 6. Test endpoints

Health:

```text
GET /
```

Webhook verification:

```text
GET /webhook/meta-leads?hub.mode=subscribe&hub.verify_token=woi_leads_verify_123&hub.challenge=12345
```

Manual sheet test:

```text
POST /test/sheet
```

Body:

```json
{
  "name": "Test Customer",
  "phone": "9876543210",
  "email": "test@example.com",
  "city": "Mysore",
  "course": "Stock Market Training"
}
```

## 7. WhatsApp later

After WhatsApp phone number is registered, add WhatsApp sending inside `process_leadgen_payload()` after `append_lead_to_sheet(row)`.


## 8. WhatsApp welcome message automation

This backend is now ready to send a WhatsApp template message immediately after a real Meta lead is fetched and written to Google Sheets.

Keep this disabled until your real WhatsApp number and template are approved:

```env
WHATSAPP_ENABLED=false
WHATSAPP_PHONE_NUMBER_ID=your_whatsapp_phone_number_id
WHATSAPP_ACCESS_TOKEN=your_permanent_whatsapp_token
WHATSAPP_TEMPLATE_NAME=hello_world
WHATSAPP_LANGUAGE_CODE=en_US
WHATSAPP_TEMPLATE_BODY_FIELDS=
```

After approval, change:

```env
WHATSAPP_ENABLED=true
WHATSAPP_TEMPLATE_NAME=your_approved_template_name
WHATSAPP_LANGUAGE_CODE=en_US
```

If your template body has variables, set them in order. Example, if template is `Hi {{1}}, thanks for registering`, use:

```env
WHATSAPP_TEMPLATE_BODY_FIELDS=name
```

For multiple variables:

```env
WHATSAPP_TEMPLATE_BODY_FIELDS=name,course,city
```

Test config:

```text
GET /debug/config
```

Test WhatsApp only:

```text
POST /test/whatsapp
```

Body:

```json
{
  "name": "Pramod",
  "phone": "919739070755",
  "course": "Stock Market Training"
}
```

Important: `/webhook/meta-leads` does not send WhatsApp messages for Meta dashboard dummy leadgen_id `444444444444444`. It sends only for real leads and skips duplicate leads already present in the sheet.
