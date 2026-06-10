from __future__ import annotations

import subprocess
from pathlib import Path

SCRIPT = Path("nginx/subscription-env.sh")
TEMPLATE = Path("nginx/templates/subscription.conf.esh")


def run_subscription_env_script(
    route: str = "s",
    *,
    legacy_routes: str = "/sub/",
    output_var: str = "VPN_SUBSCRIPTION_ROUTE_NORMALIZED",
) -> subprocess.CompletedProcess[str]:
    cert_dir = Path("/tmp/vpn-control-plane-test-certs")
    return subprocess.run(
        [
            "sh",
            "-c",
            f"mkdir -p {cert_dir} "
            f"&& : > {cert_dir}/fullchain.pem "
            f"&& : > {cert_dir}/privkey.pem "
            f"&& VPN_SUBSCRIPTION_DOMAIN=resetand.my.id VPN_SUBSCRIPTION_PORT=2096 VPN_SUBSCRIPTION_ROUTE='{route}' "
            f"VPN_SUBSCRIPTION_LEGACY_ROUTES='{legacy_routes}' "
            f"NGINX_CERT_DIR={cert_dir} "
            f". {SCRIPT.resolve()} "
            f'&& eval "printf \'%s\' \\"\\${output_var}\\""',
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def test_nginx_template_uses_official_envsubst_variables() -> None:
    template = TEMPLATE.read_text(encoding="utf-8")

    assert "listen ${VPN_SUBSCRIPTION_PORT} ssl http2;" in template
    assert "server_name ${VPN_SUBSCRIPTION_DOMAIN};" in template
    assert "location ~ ${VPN_SUBSCRIPTION_ROUTES_PATTERN}" in template
    assert "proxy_pass http://app:8080;" in template
    assert "location /" in template
    assert "/backup" not in template


def test_nginx_startup_helper_normalizes_subscription_route() -> None:
    result = run_subscription_env_script("sub")

    assert result.returncode == 0
    assert result.stdout == "/sub/"


def test_nginx_startup_helper_builds_main_and_legacy_subscription_locations() -> None:
    result = run_subscription_env_script(
        "s",
        legacy_routes="/sub/,/sub/9f3aKx7PqLm2Zr8/",
        output_var="VPN_SUBSCRIPTION_ROUTES_PATTERN",
    )

    assert result.returncode == 0
    assert result.stdout == "^(/s/|/sub/|/sub/9f3aKx7PqLm2Zr8/)"


def test_nginx_startup_helper_reports_missing_tls_files() -> None:
    missing_dir = Path("/tmp/vpn-control-plane-missing-certs")
    result = subprocess.run(
        [
            "sh",
            "-c",
            f"rm -rf {missing_dir} && VPN_SUBSCRIPTION_DOMAIN=resetand.my.id NGINX_CERT_DIR={missing_dir} "
            f". {SCRIPT.resolve()}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert f"certificate file is missing: {missing_dir}/fullchain.pem" in result.stderr
