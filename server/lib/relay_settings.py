"""Private relay provisioning, independent of the Host's primary network mode."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import shlex
import tempfile
from urllib.parse import urlsplit, urlunsplit


@dataclass(frozen=True)
class RelaySettings:
    url: str
    host_id: str
    key: str = field(repr=False)

    def __post_init__(self):
        parsed = urlsplit(self.url)
        if (parsed.scheme not in {'https', 'wss'} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or parsed.path not in {'', '/'}):
            raise ValueError('relay URL must be an HTTPS origin without credentials, path or query')
        try:
            parsed.port
        except ValueError:
            raise ValueError('invalid relay port') from None
        if not re.fullmatch(r'[a-z0-9-]{8,64}', self.host_id):
            raise ValueError('invalid relay Host ID')
        if not re.fullmatch(r'[a-f0-9]{64}', self.key):
            raise ValueError('invalid relay connector key')
        object.__setattr__(self, 'url', urlunsplit(('https', parsed.netloc, '', '', '')))

    @property
    def pairing_url(self):
        return f'{self.url}/h/{self.host_id}'

    @property
    def websocket_url(self):
        return self.url.replace('https:', 'wss:', 1)

    def public_status(self):
        return {'configured': True, 'url': self.url, 'host_id': self.host_id,
                'pairing_url': self.pairing_url}


def load(path: Path) -> RelaySettings:
    if os.name != 'nt' and path.stat().st_mode & 0o077:
        raise ValueError('relay credential file must be private (chmod 600)')
    try:
        data = json.loads(path.read_text())
        return RelaySettings(data['url'], data['host_id'], data['key'])
    except (KeyError, TypeError, json.JSONDecodeError):
        raise ValueError('invalid relay credential file') from None


def write(path: Path, settings: RelaySettings) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.relay-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as output:
            json.dump({'url': settings.url, 'host_id': settings.host_id, 'key': settings.key}, output)
            output.write('\n')
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def import_legacy(path: Path) -> RelaySettings:
    """Read the former service's env file as data, never execute shell text."""
    if os.name != 'nt' and path.stat().st_mode & 0o077:
        raise ValueError('legacy relay credentials must be private (chmod 600)')
    values = {}
    for line in path.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        key, sep, value = line.partition('=')
        if not sep:
            raise ValueError('invalid legacy relay configuration')
        tokens = shlex.split(value, comments=True)
        if len(tokens) != 1:
            raise ValueError('invalid legacy relay configuration')
        values[key.strip()] = tokens[0]
    try:
        return RelaySettings(values['CLARP_RELAY_URL'], values['CLARP_RELAY_HOST_ID'],
                             values['CLARP_RELAY_HOST_KEY'])
    except KeyError:
        raise ValueError('legacy relay configuration is incomplete') from None
