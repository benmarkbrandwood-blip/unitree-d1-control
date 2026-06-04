#!/usr/bin/env bash
# setup_env.sh is superseded by install.sh which adds sudoers and firewall guidance.
exec "$(dirname "${BASH_SOURCE[0]}")/install.sh" "$@"
