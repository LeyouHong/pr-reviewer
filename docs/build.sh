#!/usr/bin/env bash
# Render the architecture document to PDF.
#
# Chrome rather than a dedicated converter: the same engine that renders the
# HTML lays out the PDF, so what you check in a browser is what prints. The
# @page rules and break-inside hints in the source do the pagination.
set -e
cd "$(dirname "$0")"
CHROME="${CHROME:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"
"$CHROME" --headless --disable-gpu --no-pdf-header-footer \
  --print-to-pdf=pr-reviewer-architecture.pdf \
  --virtual-time-budget=4000 \
  "file://$PWD/architecture.html" 2>/dev/null
echo "wrote $PWD/pr-reviewer-architecture.pdf"
