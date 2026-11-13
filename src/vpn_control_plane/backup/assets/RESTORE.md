# Restoring the VPN control plane

`manifest.txt` says when the snapshot was taken, which nodes it covers and how many could not
be fetched. After `sh ../decrypt.sh` from the backup root, `state/` holds:

| File | Contents |
| --- | --- |
| `data.json` | Control-plane state: nodes, inbounds, clients, subscription settings. |
| `.env` | Control-plane settings and secrets: bot token, panel API tokens, subscription salt. |
| `external_subscriptions.resolved.json` | Cache of external subscription links, if the host had it. |
| `nodes/<host>-<id>-x-ui.db` | 3x-ui panel database of a node: inbounds, clients, panel settings. |
| `nodes/<host>-<id>-xray-config.json` | Xray configuration the panel generated; reference only. |
| `nodes/node-backup-errors.txt` | Present only when some node files could not be fetched. |

Not in the backup: TLS certificates (the subscription domain and the nodes) and files that
exist only on the host, such as a server-local `docker-compose.override.yml`.

## Control plane

On the target host, in a checkout of https://github.com/Resetand/vpn-control-panel:

```sh
install -m 0600 /path/to/state/.env .env
install -m 0600 /path/to/state/data.json data.json      # where VPN_HOST_DATA_FILE points
[ -f /path/to/state/external_subscriptions.resolved.json ] \
  && install -m 0600 /path/to/state/external_subscriptions.resolved.json .
```

Without `external_subscriptions.resolved.json` run `make resolve-external-subscriptions` before
the first start: otherwise subscriptions are served without the external links, and clients
that sync their server list drop those servers.

Put a certificate for `VPN_SUBSCRIPTION_DOMAIN` at `VPN_SUBSCRIPTION_CERT_PATH`, point the DNS
name at the host, then `make start`. Check `/health`, a known subscription URL and `/backup`
in the bot.

## A node panel

On the node, with the same or a newer 3x-ui installed:

```sh
systemctl stop x-ui
install -m 0600 /path/to/state/nodes/<host>-<id>-x-ui.db /etc/x-ui/x-ui.db
rm -f /etc/x-ui/x-ui.db-wal /etc/x-ui/x-ui.db-shm
systemctl start x-ui
```

The database keeps inbounds, clients, keys and the panel's own settings. Then
`make sync DRY_RUN=1` on the control plane should report no changes for that node.
