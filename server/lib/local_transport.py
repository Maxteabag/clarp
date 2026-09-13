"""Opt-in LAN HTTPS with a durable, pairable certificate identity.

The regular loopback listener remains the target for Tailscale/relay. The LAN
listener shares handlers and device revocation state but never accepts local
administrator credentials, even if reached from loopback.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import ipaddress
import os
from pathlib import Path
import socket
import ssl
import tempfile
import threading
import weakref

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


def local_ipv4_addresses() -> list[str]:
    import ifaddr
    addresses = set()
    for adapter in ifaddr.get_adapters():
        if adapter.name.startswith(("docker", "br-", "veth", "tailscale", "utun", "tun", "virbr")):
            continue
        for entry in adapter.ips:
            if not isinstance(entry.ip, str):
                continue
            try:
                address = ipaddress.IPv4Address(entry.ip)
            except ValueError:
                continue
            if any(address in net for net in (
                ipaddress.ip_network('10.0.0.0/8'), ipaddress.ip_network('172.16.0.0/12'),
                ipaddress.ip_network('192.168.0.0/16'))):
                addresses.add(str(address))
    return sorted(addresses)


def ensure_identity(directory: Path, server_id: str) -> tuple[Path, str]:
    """One atomic private PEM contains both key and certificate; never auto-rotate."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / 'identity.pem'
    if not path.exists():
        key = ec.generate_private_key(ec.SECP256R1())
        now = dt.datetime.now(dt.timezone.utc)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f'Clarp {server_id}')])
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - dt.timedelta(minutes=5))
                .not_valid_after(now + dt.timedelta(days=3650))
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .sign(key, hashes.SHA256()))
        data = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                 serialization.NoEncryption()) + cert.public_bytes(serialization.Encoding.PEM)
        fd, temporary = tempfile.mkstemp(prefix='.identity-', dir=directory)
        try:
            with os.fdopen(fd, 'wb') as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            # link rather than replace: concurrent starts cannot rotate identity.
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass
        finally:
            os.unlink(temporary)
    if path.stat().st_mode & 0o077:
        raise ValueError('local TLS identity must be private (chmod 600)')
    pem = path.read_bytes()
    cert = x509.load_pem_x509_certificate(pem)
    key = serialization.load_pem_private_key(pem, password=None)
    if key.public_key().public_numbers() != cert.public_key().public_numbers():
        raise ValueError('local TLS key does not match certificate')
    if cert.not_valid_after_utc <= dt.datetime.now(dt.timezone.utc):
        raise ValueError('local TLS certificate expired; renew and refresh pairing trust')
    return path, hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()


class LocalHTTPS:
    def __init__(self, owner, handler, identity_directory: Path, port: int, *, bind='0.0.0.0'):
        from .server_identity import get_server_info
        from .bonjour import BonjourAdvertiser
        info = get_server_info()
        self.server_id = str(info['server_id'])
        pem, self.fingerprint = ensure_identity(identity_directory, self.server_id)
        # Perform TLS handshake in request threads so a silent LAN connection
        # cannot block accept() for everybody else.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(pem)
        self.connections = weakref.WeakSet()
        self.connections_lock = threading.Lock()
        connections = self.connections
        connections_lock = self.connections_lock
        class TLSHandler(handler):
            def setup(self):
                self.request.settimeout(5)
                self.request = context.wrap_socket(self.request, server_side=True)
                with connections_lock:
                    connections.add(self.request)
                super().setup()

        class TLSServer(type(owner)):
            slots = threading.BoundedSemaphore(128)
            def process_request(self, request, client_address):
                if not self.slots.acquire(blocking=False):
                    self.shutdown_request(request)
                    return
                try:
                    super().process_request(request, client_address)
                except BaseException:
                    self.slots.release()
                    raise
            def process_request_thread(self, request, client_address):
                try:
                    super().process_request_thread(request, client_address)
                finally:
                    self.slots.release()

        self.server = TLSServer((bind, port), TLSHandler, owner.ctx)
        self.server.local_tls = True
        self.server.auth_failures = owner.auth_failures
        self.server._device_connections = owner._device_connections
        self.server._device_connections_lock = owner._device_connections_lock
        self.server.local_transport = self
        self.thread = threading.Thread(target=self.server.serve_forever, name='clarp-local-https', daemon=True)
        self.advertiser = BonjourAdvertiser(name=str(info['name']), server_id=self.server_id,
            port=self.server.server_port, auth_required=True, secure=True)

    def start(self):
        self.thread.start()
        self.advertiser.start()

    def description(self):
        port = self.server.server_port
        return {'enabled': True, 'port': port, 'certificate_sha256': self.fingerprint,
                'service_type': '_clarps._tcp',
                'urls': [f'https://{ip}:{port}' for ip in local_ipv4_addresses()]}

    def close(self):
        self.advertiser.stop()
        self.server.shutdown()
        with self.connections_lock:
            active = list(self.connections)
        for connection in active:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self.server.server_close()
        self.thread.join(timeout=5)
