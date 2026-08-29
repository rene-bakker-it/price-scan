#!/usr/bin/env bash
set -o pipefail

SCRIPT_NAME=$1
if [[ "${SCRIPT_NAME}" != "trains" && "${SCRIPT_NAME}" != "flights" ]]; then
    echo "Invalid script name. Please specify either 'trains' or 'flights'."
    exit 1
fi
shift

DEPART="$1"
if [[ -z "${DEPART}" ]]; then
    echo "Invalid departure. For flights provide a valid 3-letter IATA code. For train stations, a valid name on its web-site."
    exit 1
fi
shift
ARRIVE="$1"
if [[ -z "${ARRIVE}" ]]; then
    echo "Invalid arrival. For flights provide a valid 3-letter IATA code. For train stations, a valid name on its web-site."
    exit 1
fi
shift

MY_DATE="$1"
if [[ ! "${MY_DATE}" =~ ^20[0-9]{2}-[0-9]{2}-[0-9]{2}$ ]]; then
    echo "Invalid date format. Please provide a date in the format YYYY-MM-DD."
    exit 1
fi
shift

export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
cd /app || exit
if [[ "${SCRIPT_NAME}" == "flights"  ]]; then
  timeout --signal=TERM --kill-after=10s 120s uv run flights.py "$DEPART" "$ARRIVE" "$MY_DATE" "$@"
  status=${PIPESTATUS[0]}
  if [[ ${status} -eq 124 || ${status} -eq 137 ]]; then
      echo "flights.py '$DEPART' '$ARRIVE' $MY_DATE exceeded 2 minutes and was killed"
  fi
else
  export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-${HOME}/.cache/ms-playwright}"
  timeout --signal=TERM --kill-after=10s 120s uv run frecce.py "$DEPART" "$ARRIVE" "$MY_DATE" "$@"
  status=${PIPESTATUS[0]}
  if [[ ${status} -eq 124 || ${status} -eq 137 ]]; then
      echo "frecce.py '$DEPART' '$ARRIVE' $MY_DATE exceeded 2 minutes and was killed"
  fi
  timeout --signal=TERM --kill-after=10s 180s uv run italo.py "$DEPART" "$ARRIVE" "$MY_DATE" "$@"
  status=${PIPESTATUS[0]}
  if [[ ${status} -eq 124 || ${status} -eq 137 ]]; then
      echo "italo.py '$DEPART' '$ARRIVE' $MY_DATE exceeded 3 minutes and was killed"
  fi
fi
