# PrintPilot — Facebook Messenger Printing Assistant

Printing-order automation prepared for Facebook Page ID **61595456493475**.

## Functions
- Guided Messenger ordering: paper size, margins, color or B&W, sides, orientation, paper, copies, pages, pickup or delivery
- File inspection and estimated printing price
- Password-protected dashboard, order status, and pricing updates

## Deployment
The application is packaged in split, base64-encoded `runtime.part*` files and rebuilt by `Dockerfile` during Render deployment. No secrets or customer documents are stored in Git.

**Important:** Live Messenger requires authorized Facebook Page credentials, a public HTTPS webhook, and Meta app permissions. Do not upload your Page token, Meta app secret, administrator password, or customer documents to GitHub.

**Production storage:** SQLite data and uploaded documents require persistent storage. Free Render instances do not provide durable storage; do not accept real customer orders until persistent disk and backups are configured.

The application is not live merely because this repository exists.
