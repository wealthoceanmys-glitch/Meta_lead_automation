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

## WhatsApp seminar automation

This build automatically selects the next seminar date/time when a Meta lead arrives.

Seminar schedule:

- Thursday evening: 6:00 PM to 8:00 PM, arrive by 5:45 PM
- Sunday morning: 10:30 AM to 12:30 PM, arrive by 10:15 AM

How it chooses the seminar:

1. If the lead form/ad/campaign data mentions Thursday, it sends the next upcoming Thursday seminar.
2. If the lead form/ad/campaign data mentions Sunday, it sends the next upcoming Sunday seminar.
3. If no day is found, it sends the nearest upcoming seminar.

Recommended Render environment variables:

```env
WHATSAPP_ENABLED=true
WHATSAPP_ACCESS_TOKEN=your_system_user_token
WHATSAPP_PHONE_NUMBER_ID=1104734059391596
WHATSAPP_TEMPLATE_NAME=woi_seminar_registration_followup
WHATSAPP_LANGUAGE_CODE=en
WHATSAPP_TEMPLATE_BODY_FIELDS=customer_name,seminar_date,seminar_time,arrival_time,venue

SEMINAR_TIMEZONE=Asia/Kolkata
SEMINAR_VENUE=Kuvempunagara, Mysuru - https://g.co/kgs/FDbcqh
SEMINAR_THURSDAY_TIME=6:00 PM to 8:00 PM
SEMINAR_THURSDAY_ARRIVAL=5:45 PM
SEMINAR_SUNDAY_TIME=10:30 AM to 12:30 PM
SEMINAR_SUNDAY_ARRIVAL=10:15 AM
```

Your WhatsApp template body should contain these named variables:

```text
{{customer_name}}
{{seminar_date}}
{{seminar_time}}
{{arrival_time}}
{{venue}}
```

## WhatsApp delivery/read status and incoming replies

This version also handles WhatsApp Cloud API `messages` webhooks on the same callback URL:

```text
https://YOUR_RENDER_URL.onrender.com/webhook/meta-leads
```

### What it updates in the `Leads` sheet

The backend will automatically add these columns if they are missing:

- `whatsapp_message_id`
- `whatsapp_sent`
- `whatsapp_delivered`
- `whatsapp_read`
- `whatsapp_failed`
- `whatsapp_status`
- `whatsapp_error`
- `whatsapp_sent_at`
- `whatsapp_delivered_at`
- `whatsapp_read_at`
- `whatsapp_failed_at`
- `whatsapp_last_status_at`
- `whatsapp_conversation_id`
- `whatsapp_reply_received`
- `whatsapp_reply_text`
- `whatsapp_reply_from`
- `whatsapp_reply_at`
- `whatsapp_reply_message_id`

When a lead is written and WhatsApp send succeeds, the same lead row is updated with:

```text
whatsapp_sent = Yes
whatsapp_message_id = wamid...
whatsapp_status = accepted
```

When WhatsApp later sends status webhooks, the same row is updated:

```text
sent      -> whatsapp_sent = Yes
delivered -> whatsapp_delivered = Yes
read      -> whatsapp_read = Yes
failed    -> whatsapp_failed = Yes
```

### Incoming replies

If the user replies to the WhatsApp automation number, the reply will be saved in two places:

1. Same `Leads` row, latest reply columns:
   - `whatsapp_reply_received`
   - `whatsapp_reply_text`
   - `whatsapp_reply_from`
   - `whatsapp_reply_at`
   - `whatsapp_reply_message_id`

2. Separate sheet tab named:

```text
WhatsApp Messages
```

This tab keeps all incoming replies as a log.

### Meta setup required

In Meta Developer App, configure WhatsApp webhooks and subscribe to the `messages` webhook field for your WhatsApp Business Account.

Use the same callback URL and verify token already used by your app:

```text
Callback URL: https://YOUR_RENDER_URL.onrender.com/webhook/meta-leads
Verify token: same META_VERIFY_TOKEN in Render
Webhook field: messages
```

### Manual status test

After sending a test WhatsApp message, copy its `wamid...` from the Google Sheet `whatsapp_message_id` column and call:

```bash
curl -X POST "https://YOUR_RENDER_URL.onrender.com/test/whatsapp-status" ^
-H "Content-Type: application/json" ^
-d "{\"message_id\":\"wamid.YOUR_ID\",\"status\":\"delivered\"}"
```

This only tests Google Sheet updating. Real delivered/read updates require WhatsApp `messages` webhook subscription in Meta.
