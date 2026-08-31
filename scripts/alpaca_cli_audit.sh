#!/usr/bin/env bash
set -u

PHASE="${1:-snapshot}"
STAMP="$(date -u +'%Y%m%dT%H%M%SZ')"
OUTDIR="data/cli_audit/${STAMP}"

mkdir -p "${OUTDIR}"

capture() {
    local name="$1"
    shift

    echo "Capturing ${name}..."

    if "$@" > "${OUTDIR}/${PHASE}_${name}.json" \
             2> "${OUTDIR}/${PHASE}_${name}.err"; then

        if [ ! -s "${OUTDIR}/${PHASE}_${name}.err" ]; then
            rm -f "${OUTDIR}/${PHASE}_${name}.err"
        fi
    fi
}

# READ-ONLY Alpaca CLI evidence.
capture "clock" alpaca clock
capture "account" alpaca account get
capture "positions" alpaca position list
capture "orders" alpaca order list --status all

cat > "${OUTDIR}/${PHASE}_manifest.json" <<EOF
{
  "phase": "${PHASE}",
  "timestamp_utc": "${STAMP}",
  "paper_trading": true,
  "execution_authority": "read_only_cli_audit"
}
EOF

echo "CLI audit written to ${OUTDIR}"
