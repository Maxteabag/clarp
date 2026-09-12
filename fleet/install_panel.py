"""Install the hidden fleet panel and add its badge without replacing other Waybar modules."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    home = Path.home()
    config = home / '.config/waybar/config.jsonc'
    original = config.read_text()
    # Refuse unsupported JSONC rather than stripping comments or changing unrelated data.
    data = json.loads(original)
    module = {'exec': str(home / '.local/bin/clarp-fleet') + ' badge', 'return-type': 'json',
              'interval': 5, 'on-click': str(home / '.local/bin/clarp-fleet-panel'), 'tooltip': True}
    if 'custom/fleet' in data and data['custom/fleet'] != module:
        raise ValueError('Existing unrelated fleet module; reconcile before changing')
    data['custom/fleet'] = module
    if 'custom/fleet' not in data['modules-left']:
        data['modules-left'].insert(1, 'custom/fleet')
    if not args.apply:
        print(json.dumps({'module': module, 'modules-left': data['modules-left'], 'panel_starts_hidden': True}))
        return
    if not shutil.which('qs'):
        raise ValueError('Quickshell is required')
    panel = home / '.config/quickshell/clarp-fleet/shell.qml'
    panel.parent.mkdir(parents=True, exist_ok=True)
    if panel.exists() and 'managed-by-clarp-fleet' not in panel.read_text():
        raise ValueError('Unrelated panel already exists')
    panel.write_text('// managed-by-clarp-fleet\n' + (Path(__file__).parent / 'qml/shell.qml').read_text())
    launcher = home / '.local/bin/clarp-fleet-panel'
    if launcher.exists() and 'managed-by-clarp-fleet' not in launcher.read_text():
        raise ValueError('Unrelated panel launcher exists')
    launcher.write_text('''#!/bin/sh
# managed-by-clarp-fleet
systemctl --user start clarp-fleet-panel.service || exit
attempt=0
while [ "$attempt" -lt 20 ]; do
  if /usr/bin/qs ipc --path "$HOME/.config/quickshell/clarp-fleet/shell.qml" show >/dev/null 2>&1; then
    exec /usr/bin/qs ipc --path "$HOME/.config/quickshell/clarp-fleet/shell.qml" call fleet toggle
  fi
  attempt=$((attempt + 1))
  sleep 0.1
done
exit 1
''')
    launcher.chmod(0o755)
    unit = home / '.config/systemd/user/clarp-fleet-panel.service'
    if unit.exists() and 'managed-by-clarp-fleet' not in unit.read_text():
        raise ValueError('Unrelated panel service exists')
    unit.write_text('''# managed-by-clarp-fleet
[Unit]
Description=Clarp compute fleet panel
After=graphical-session.target
PartOf=graphical-session.target
[Service]
ExecStart=/usr/bin/qs --no-duplicate --path %h/.config/quickshell/clarp-fleet/shell.qml
Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin
Restart=on-failure
RestartSec=3
[Install]
WantedBy=graphical-session.target
''')
    backup = home / '.local/state/clarp-fleet' / ('waybar-before-' + str(time.time_ns()) + '.jsonc')
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_text(original)
    if config.read_text() != original:
        raise ValueError('Waybar changed concurrently; retry after reconciliation')
    config.write_text(json.dumps(data, indent=2) + '\n')
    subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', '--user', 'enable', '--now', 'clarp-fleet-panel.service'], check=True)
    print(json.dumps({'installed': True, 'backup': str(backup), 'waybar_reload_required': True,
                      'panel_starts_hidden': True}))


if __name__ == '__main__':
    main()
