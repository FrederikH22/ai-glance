#!/usr/bin/env bash
# Install (or --uninstall) the ai-glance Terminator plugin for the current user.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF="${XDG_CONFIG_HOME:-$HOME/.config}/terminator"
PLUGINS="$CONF/plugins"

# Edit [global_config] enabled_plugins with configobj directly: unlike
# terminatorlib's Config.save() it keeps comments and unknown keys, and failures
# are visible. The config is backed up to config.bak-ai-glance before every edit
# and re-read afterwards; any mismatch exits non-zero.
edit_config() {  # $1 = add | remove
    python3 - "$1" "$CONF/config" <<'PY'
import os
import shutil
import sys

from configobj import ConfigObj
from terminatorlib.config import DEFAULTS

mode, path = sys.argv[1], sys.argv[2]
want = mode == 'add'


def read_plugins(cfg):
    section = cfg.get('global_config', {})
    if 'enabled_plugins' not in section:
        return None
    value = section['enabled_plugins']
    if isinstance(value, str):
        value = [value] if value else []
    return [str(p) for p in value]


exists = os.path.exists(path)
cfg = ConfigObj(path, encoding='utf-8') if exists else ConfigObj(encoding='utf-8')
current = read_plugins(cfg)
if current is None:
    if not want:
        sys.exit(0)  # nothing was ever enabled by us
    current = list(DEFAULTS['global_config']['enabled_plugins'])
plugins = [p for p in current if p != 'AiGlance']
if want:
    plugins.append('AiGlance')
if ('AiGlance' in current) == want and read_plugins(cfg) is not None:
    sys.exit(0)  # already as intended: leave the file and the backup untouched

if exists:
    shutil.copy2(path, os.path.join(os.path.dirname(path), 'config.bak-ai-glance'))
if 'global_config' not in cfg:
    cfg['global_config'] = {}
cfg['global_config']['enabled_plugins'] = plugins
tmp = path + '.ai-glance-tmp'
with open(tmp, 'wb') as fh:
    cfg.write(fh)
if exists:
    shutil.copymode(path, tmp)
os.replace(tmp, path)

after = read_plugins(ConfigObj(path, encoding='utf-8'))
if after is None or ('AiGlance' in after) != want:
    sys.exit('ai-glance: Terminator config does not contain the intended '
             'enabled_plugins after writing %s (backup: config.bak-ai-glance)' % path)
PY
}

if [ "${1:-}" = "--uninstall" ]; then
    edit_config remove
    rm -f "$PLUGINS/ai_glance_plugin.py" "$PLUGINS/ai_glance"
    echo "ai-glance removed. Restart Terminator."
    exit 0
fi

mkdir -p "$PLUGINS"
ln -sfn "$HERE/ai_glance_plugin.py" "$PLUGINS/ai_glance_plugin.py"
ln -sfn "$HERE/ai_glance" "$PLUGINS/ai_glance"
edit_config add
echo "ai-glance installed. Restart Terminator (running instances keep the old behaviour)."
