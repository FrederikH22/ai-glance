#!/usr/bin/env bash
# Install (or --uninstall) the ai-glance Terminator plugin for the current user.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF="${XDG_CONFIG_HOME:-$HOME/.config}/terminator"
PLUGINS="$CONF/plugins"

edit_config() {  # $1 = add | remove
    python3 - "$1" <<'PY'
import sys
import types
from terminatorlib.config import Config

cfg = Config()
# Config.save() reads command-line options, which only exist inside Terminator.
cfg.options_set(types.SimpleNamespace(config=None))
plugins = list(cfg['enabled_plugins'])
if sys.argv[1] == 'add':
    if 'AiGlance' not in plugins:
        plugins.append('AiGlance')
else:
    plugins = [p for p in plugins if p != 'AiGlance']
cfg['enabled_plugins'] = plugins
if cfg.save() is False:
    sys.exit('could not save the Terminator config')
PY
}

if [ "${1:-}" = "--uninstall" ]; then
    rm -f "$PLUGINS/ai_glance_plugin.py" "$PLUGINS/ai_glance"
    edit_config remove
    echo "ai-glance removed. Restart Terminator."
    exit 0
fi

mkdir -p "$PLUGINS"
# keep the first backup: it is the config as it was before ai-glance touched it
if [ -f "$CONF/config" ] && [ ! -e "$CONF/config.bak-ai-glance" ]; then
    cp -p "$CONF/config" "$CONF/config.bak-ai-glance"
fi
ln -sfn "$HERE/ai_glance_plugin.py" "$PLUGINS/ai_glance_plugin.py"
ln -sfn "$HERE/ai_glance" "$PLUGINS/ai_glance"
edit_config add
echo "ai-glance installed. Restart Terminator (running instances keep the old behaviour)."
