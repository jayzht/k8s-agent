#!/bin/bash
# usage: fetch.sh <url> <outname>
UA="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
URL="$1"; OUT="$2"
DIR="$(dirname "$0")/pages"
mkdir -p "$DIR"
CODE=$(curl -sL --max-time 45 -A "$UA" -H "Accept-Language: en-US,en;q=0.9" -w "%{http_code}" -o "$DIR/$OUT.html" "$URL")
python3 "$(dirname "$0")/h2t.py" < "$DIR/$OUT.html" > "$DIR/$OUT.txt" 2>/dev/null
echo "$OUT http=$CODE bytes=$(wc -c < "$DIR/$OUT.txt")"
