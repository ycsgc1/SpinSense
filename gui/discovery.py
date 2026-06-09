"""mDNS/zeroconf advertisement of the SpinSense HTTP service so the companion
Home Assistant integration can auto-discover it on the LAN.

Runs inside the FastAPI (GUI) process. All failures are non-fatal: if zeroconf
cannot bind (no network, UDP 5353 in use), we log and carry on serving HTTP.
"""
import logging
import os
import socket

from zeroconf import ServiceInfo
from zeroconf.asyncio import AsyncZeroconf

log = logging.getLogger(__name__)

SERVICE_TYPE = "_spinsense._tcp.local."


def _read_version() -> str:
    """The app version from the repo-root VERSION file, advertised in the mDNS
    TXT record. Mirrors backend_main's ASSET_VERSION read."""
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "..", "VERSION")) as f:
            return f.read().strip() or "dev"
    except OSError:
        return "dev"


def get_port() -> int:
    try:
        return int(os.environ.get("SPINSENSE_PORT", "3313"))
    except (TypeError, ValueError):
        return 3313


def is_enabled(config: dict) -> bool:
    return bool(
        (config or {}).get("Discovery", {}).get("mDNS", {}).get("Enabled", True)
    )


def _hostname() -> str:
    return socket.gethostname().split(".")[0] or "spinsense"


def _local_ip() -> str | None:
    """Best-effort primary LAN IPv4. The UDP 'connect' sends no packets; it just
    selects the default-route source address. Returns None if undiscoverable."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            return ip if ip and not ip.startswith("127.") else None
        finally:
            s.close()
    except Exception:
        return None


def _instance_name(service_name: str) -> str:
    name = (service_name or "").strip()
    if not name:
        name = f"SpinSense ({_hostname()})"
    return f"{name}.{SERVICE_TYPE}"


def build_service_info(port: int, service_name: str, version: str) -> ServiceInfo:
    addresses = []
    ip = _local_ip()
    if ip:
        addresses = [socket.inet_aton(ip)]
    return ServiceInfo(
        type_=SERVICE_TYPE,
        name=_instance_name(service_name),
        port=port,
        properties={"version": version, "path": "/"},
        server=f"{_hostname()}.local.",
        addresses=addresses,
    )


class Advertiser:
    """Owns the AsyncZeroconf instance and the currently-registered service."""

    def __init__(self, version: str | None = None):
        version = version if version is not None else _read_version()
        self._azc: AsyncZeroconf | None = None
        self._info: ServiceInfo | None = None
        self._version = version

    async def reconcile(self, config: dict) -> None:
        """Make the live advertisement match config: register if enabled and not
        yet registered, unregister if disabled. Never raises."""
        try:
            if is_enabled(config):
                await self._ensure_registered(config)
            else:
                await self.stop()
        except Exception as exc:
            log.warning("mDNS reconcile failed: %s", exc)

    async def _ensure_registered(self, config: dict) -> None:
        if self._info is not None:
            return  # already advertising
        service_name = (
            (config or {}).get("Discovery", {}).get("mDNS", {}).get("Service_Name", "")
        )
        info = build_service_info(get_port(), service_name, self._version)
        if self._azc is None:
            self._azc = AsyncZeroconf()
        await self._azc.async_register_service(info)
        self._info = info
        log.info("mDNS advertising %s on port %s", info.name, info.port)

    async def start(self, config: dict) -> None:
        await self.reconcile(config)

    async def stop(self) -> None:
        try:
            if self._azc is not None and self._info is not None:
                await self._azc.async_unregister_service(self._info)
            if self._azc is not None:
                await self._azc.async_close()
        except Exception as exc:
            log.warning("mDNS stop failed: %s", exc)
        finally:
            self._azc = None
            self._info = None


advertiser = Advertiser()
