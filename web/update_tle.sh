#!/bin/sh
# Refresh web/tle.txt (orbital elements for the Satellites tab) from Celestrak. Celestrak sends no CORS
# headers, so the browser cannot fetch them itself; this file is committed and refreshed by the tle workflow.
set -e
cd "$(dirname "$0")"
out=tle.txt.new; : > $out
for q in "CATNR=25544" "GROUP=amateur" "GROUP=weather" "GROUP=orbcomm"; do
  curl -fsS "https://celestrak.org/NORAD/elements/gp.php?$q&FORMAT=tle" >> $out || exit 1
  printf '\n' >> $out
done
grep -c '^1 ' $out > /dev/null && mv $out tle.txt && echo "tle.txt: $(grep -c '^1 ' tle.txt) satellites, $(date -u +%Y-%m-%dT%H:%MZ)"
