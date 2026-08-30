#!/usr/bin/env bash

SRC_FILE=/data/data.db

if [ -f "$SRC_FILE" ]; then
  DATE_STR=$(date +%Y%m%d)
  DEST_FILE="/data/data_${DATE_STR}.db"
  cp "$SRC_FILE" "$DEST_FILE" >  /dev/null 2>&1
else
  echo "WARNING: file '${SRC_FILE}' not found."
fi

