fail() {
    echo "nginx config error: $1" >&2
    return 1
}

if [ -z "${VPN_SUBSCRIPTION_DOMAIN:-}" ]; then
    fail "VPN_SUBSCRIPTION_DOMAIN is required" || return 1 2>/dev/null || exit 1
fi

VPN_SUBSCRIPTION_PORT="${VPN_SUBSCRIPTION_PORT:-443}"
case "$VPN_SUBSCRIPTION_PORT" in
    ''|*[!0-9]*)
        fail "VPN_SUBSCRIPTION_PORT must be a number between 1 and 65535" || return 1 2>/dev/null || exit 1
        ;;
esac
if [ "$VPN_SUBSCRIPTION_PORT" -lt 1 ] || [ "$VPN_SUBSCRIPTION_PORT" -gt 65535 ]; then
    fail "VPN_SUBSCRIPTION_PORT must be between 1 and 65535" || return 1 2>/dev/null || exit 1
fi

route=$(printf '%s' "${VPN_SUBSCRIPTION_ROUTE:-/sub/}" | sed 's#^/*##; s#/*$##')
if [ -z "$route" ]; then
    fail "VPN_SUBSCRIPTION_ROUTE must not be empty" || return 1 2>/dev/null || exit 1
fi

cert_dir="${NGINX_CERT_DIR:-/etc/nginx/certs}"
if [ ! -f "$cert_dir/fullchain.pem" ]; then
    fail "certificate file is missing: $cert_dir/fullchain.pem" || return 1 2>/dev/null || exit 1
fi
if [ ! -f "$cert_dir/privkey.pem" ]; then
    fail "certificate file is missing: $cert_dir/privkey.pem" || return 1 2>/dev/null || exit 1
fi

export VPN_SUBSCRIPTION_PORT
export VPN_SUBSCRIPTION_ROUTE_NORMALIZED="/$route/"