# Smoke Checklist

Use this checklist against a disposable 3x-UI test node before cutover. Run it with private runtime data and environment secrets, not committed sample data.

## Prerequisites

- `make init` has created `.env` and the runtime `data.json` file.
- `.env` contains `VPN_SUBSCRIPTION_ROUTE`, `VPN_SUBSCRIPTION_DOMAIN`, `VPN_SUBSCRIPTION_PORT`, `VPN_SUBSCRIPTION_CERT_PATH`, `VPN_TELEGRAM_BOT_TOKEN`, `VPN_TELEGRAM_ALLOWED_USER_IDS` or `VPN_TELEGRAM_ALLOWED_CHAT_ID`, `VPN_TELEGRAM_ADMIN_IDS`, `VPN_DEFAULT_VLESS_FLOW`, `BACKUP_HTTP_TOKEN`, optional `BACKUP_SECRETS_SSH_KEY`, and every secret referenced by `${{ env.VAR_NAME }}` in JSON data.
- `VPN_SUBSCRIPTION_CERT_PATH` points to a directory with `fullchain.pem` and `privkey.pem`.
- `nginx/templates/subscription.conf.esh` is present and `make up` starts the bundled nginx service successfully.
- `data.json` points to the 3x-UI test node panel API under `nodes[]`.
- `data.json` includes at least one managed `nodes[].inbounds[]` item and one `externalInbounds[]` raw URI.
- The public subscription route is configured to the exact legacy path that existing clients use.
- Existing clients that need old public URL compatibility have `legacySubId` in `data.json` before stack restart.

## Provision A New Client

1. Start the stack with `make up` and confirm `curl http://127.0.0.1:8080/health` returns `{"status":"ok"}`.
2. From an allowed Telegram user, send `/start` in a private chat.
3. Confirm the bot replies with one subscription URL and one QR code.
4. In 3x-UI, confirm a new client exists for every managed inbound in the effective client tag list.
5. Confirm each 3x-UI client email is exactly `<xuiInboundId>_<telegram_user_id>` and `subId` equals the subscription identifier.
6. Open the subscription URL in a VPN client and confirm the node-generated links connect successfully.
7. Confirm the new record in `data.json` has `subId` and does not have `legacySubId`.

## Returning User Idempotency

1. Send `/start` again from the same Telegram user.
2. Confirm the bot returns the same subscription URL.
3. In 3x-UI, confirm no duplicate clients were created.
4. Confirm existing UUID/password key material for that user did not change.

## Subscription Compatibility

1. Request the exact public subscription path directly, for example `curl -i "https://$VPN_SUBSCRIPTION_DOMAIN/$VPN_SUBSCRIPTION_ROUTE/<subId>"` with duplicate slashes cleaned up if your shell expands the route with a trailing slash.
2. Confirm the response is HTTP 200 without redirect.
3. Base64-decode the body and confirm it contains links generated from 3x-UI inbound API data plus the raw `externalInbounds[]` URI.
4. Confirm decoded links end with a trailing newline.
5. Confirm link order matches the requested client's `inboundTags`, or `defaultClientInboundTags` when the client has no override.
6. Confirm response headers include configured metadata such as `profile-title`, `profile-update-interval`, `profile-web-page-url`, `support-url`, `routing`, and `routing-enable`.
7. For a migrated client, request `legacySubId` and confirm the response is HTTP 302 to the random `subId` URL.

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
2. Confirm the archive contains only `data.json`.
3. Confirm the archive does not contain `.env`, logs, caches, Telegram tokens, or raw environment secret values.
4. Send `/backup` as a Telegram admin and confirm the bot sends one `vpn-control-plane-backup.tar.gz` archive.
5. Confirm the Telegram archive contains `data.json`, 3x-UI node files, and `env.encrypted` when `BACKUP_SECRETS_SSH_KEY` is configured.
6. Copy the Telegram archive to a machine with the matching private key and confirm `tar -xzOf vpn-control-plane-backup.tar.gz env.encrypted | age -d -i ~/.ssh/id_ed25519 | tar -tzf -` lists `.env`.
