# PrintPilot deployment (Facebook Page 61595456493475)

## Status
This is a starter deployment. Messenger is **not connected** until you enter the official Meta secrets into Render. Do not commit those secrets to GitHub. The /health endpoint reports whether required secrets exist.

## Essential setup
1. In Render, set environment variables PAGE_ACCESS_TOKEN, META_APP_SECRET, META_VERIFY_TOKEN and ADMIN_PASSWORD (at least 12 characters). Use **Environment** to set them privately.
2. The Render service URL plus /webhook is your Meta Webhook Callback URL. The Verify Token must match META_VERIFY_TOKEN.
3. Configure the Meta Developer App and appropriate Page subscriptions/permissions to receive messages.
4. Access /admin using ADMIN_USERNAME and ADMIN_PASSWORD over HTTPS.
5. Test with an authorized Page account and attached sample document.
6. On paid Render with persistent disk, point DATA_DIR to the attached persistent disk path. On free hosting /tmp/printpilot is **ephemeral** — uploaded documents and orders can disappear after restarts or redeployment. Do not use free deployment for real customer orders.

## Security
Your GitHub repo is public; only source code (never real files, credentials, or customer data) belongs there. This application verifies Meta webhook HMAC signatures and requires HTTP Basic admin authentication. Human review is required for Office documents and final print layouts.

## Costs
This file uses free hosting for limited technical testing. Permanent storage usually requires upgrading to a paid service or attaching an external durable database and object storage. Do not activate production order intake until that is configured.

## Limitations
Office files are accepted, but page counts and layout must be verified manually. Automatic pricing is an estimate and can be adjusted with PRICE_BW and PRICE_COLOR env vars.
