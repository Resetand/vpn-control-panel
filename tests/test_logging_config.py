import logging

from vpn_control_plane.app import NOISY_LOGGERS, configure_logging


def test_configure_logging_silences_per_request_http_loggers() -> None:
    configure_logging()

    for name in NOISY_LOGGERS:
        assert logging.getLogger(name).level == logging.WARNING
    # Application loggers keep following the root level instead of being capped.
    assert logging.getLogger("vpn_control_plane").level == logging.NOTSET
