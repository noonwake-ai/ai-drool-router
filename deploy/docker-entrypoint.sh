#!/bin/sh

# SPDX-FileCopyrightText: 2026 NoonWake.AI
# SPDX-License-Identifier: LGPL-3.0-or-later
# Small dispatch layer so one image can act as the web process, the worker, the
# metadata sync, or the rate tick. Anything else is executed verbatim.
set -eu

case "${1:-web}" in
  web)
    exec python -m detector.server
    ;;
  worker)
    exec python -m detector.monitor --source timer
    ;;
  sync)
    exec python -m detector.monitor --source timer --metadata-only
    ;;
  pricing)
    exec python -m detector.pricing_tick
    ;;
  preview)
    # Offline demo data. Never contacts a gateway and never needs a key.
    exec python scripts/dev_preview.py --host 0.0.0.0
    ;;
  *)
    exec "$@"
    ;;
esac
