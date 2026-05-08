# Smoke Checklist

Use this checklist against a disposable 3x-UI test node before cutover. Run it with private runtime data and environment secrets, not committed sample data.

## Prerequisites

- `make init` has created `.env` and the runtime data directory.
- `.env` contains `VPN_SUBSCRIPTION_ROUTE`, `VPN_SUBSCRIPTION_DOMAIN`, `VPN_SUBSCRIPTION_PORT`, `VPN_SUBSCRIPTION_CERT_PATH`, `VPN_TELEGRAM_BOT_TOKEN`, `VPN_TELEGRAM_ALLOWED_USER_IDS` or `VPN_TELEGRAM_ALLOWED_CHAT_ID`, `VPN_TELEGRAM_ADMIN_IDS`, `VPN_DEFAULT_VLESS_FLOW`, `BACKUP_HTTP_TOKEN`, optional `BACKUP_SECRETS_SSH_KEY`, and every secret referenced by `${{ env.VAR_NAME }}` in JSON data.
- `VPN_SUBSCRIPTION_CERT_PATH` points to a directory with `fullchain.pem` and `privkey.pem`.
- `nginx/templates/subscription.conf.esh` is present and `make up` starts the bundled nginx service successfully.
- `nodes.json` points to the 3x-UI test node and has a working `subscriptionBaseUrl`.
- `inbounds.json` includes at least one `node-inbound` for the test node and one `external-inbound` raw URI.
- The public subscription route is configured to the exact legacy path that existing clients use.

## Provision A New Client

1. Start the stack with `make up` and confirm `curl http://127.0.0.1:8080/health` returns `{"status":"ok"}`.
2. From an allowed Telegram user, send `/start` in a private chat.
3. Confirm the bot replies with one subscription URL and one QR code.
4. In 3x-UI, confirm a new client exists for every configured `node-inbound`.
5. Confirm each 3x-UI client email is exactly `<inbound_id>_<telegram_user_id>` and `subId` equals the subscription identifier.
6. Open the subscription URL in a VPN client and confirm the node-generated links connect successfully.

## Returning User Idempotency

1. Send `/start` again from the same Telegram user.
2. Confirm the bot returns the same subscription URL.
3. In 3x-UI, confirm no duplicate clients were created.
4. Confirm existing UUID/password key material for that user did not change.

## Subscription Compatibility

1. Request the exact public subscription path directly, for example `curl -i "https://$VPN_SUBSCRIPTION_DOMAIN/$VPN_SUBSCRIPTION_ROUTE/<subId>"` with duplicate slashes cleaned up if your shell expands the route with a trailing slash.
2. Confirm the response is HTTP 200 without redirect.
3. Base64-decode the body and confirm it contains node-generated links plus the raw `external-inbound` URI.
4. Confirm decoded links end with a trailing newline.
5. Confirm link order matches the item order in `inbounds.json`.
6. Confirm response headers include configured metadata such as `profile-title`, `profile-update-interval`, `profile-web-page-url`, `support-url`, `routing`, and `routing-enable`.

## Public Surface

1. Confirm TLS reaches nginx: `curl -i "https://$VPN_SUBSCRIPTION_DOMAIN:$VPN_SUBSCRIPTION_PORT$VPN_SUBSCRIPTION_ROUTE<subId>"`.
2. Confirm protected routes are not public: `curl -i "https://$VPN_SUBSCRIPTION_DOMAIN:$VPN_SUBSCRIPTION_PORT/backup"` returns 404 from nginx.
3. Confirm the local backup route still requires the bearer token on `http://127.0.0.1:8080/backup`.

## Announcement Headers

1. Send `/announce Maintenance tonight` as a Telegram admin.
2. Request a known subscription URL and confirm the `announce` response header is present and starts with `base64:`.
3. Decode the `announce` header and confirm it equals `Maintenance tonight`.
4. Send `/unannounce` as a Telegram admin.
5. Request the same subscription URL and confirm the `announce` header is absent.

## Backup

1. Download a backup with `curl -H "Authorization: Bearer $BACKUP_HTTP_TOKEN" -o data.tar.gz http://127.0.0.1:8080/backup`.
2. Confirm the archive contains only `nodes.json`, `clients.json`, `inbounds.json`, and `subscription.json`.
3. Confirm the archive does not contain `.env`, logs, caches, Telegram tokens, or raw environment secret values.
4. If `BACKUP_SECRETS_SSH_KEY` is configured, send `/backup` as a Telegram admin and confirm the bot sends both the regular data archive and `backup.secrets`.
5. Copy `backup.secrets` to a machine with the matching private key and confirm `age -d -i ~/.ssh/id_ed25519 -o secrets.tar.gz backup.secrets` decrypts successfully.
