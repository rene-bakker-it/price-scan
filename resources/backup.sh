#!/usr/bin/env bash

SRC_FILE=/data/data.db

if [ -f "$SRC_FILE" ]; then
  DATE_STR=$(date +%Y%m%d)
  DEST_FILE="/backup/data_${DATE_STR}.db"
  cp "$SRC_FILE" "$DEST_FILE" >  /dev/null 2>&1
fi

