#!/usr/bin/env bash
# End-to-end demo of the report reassignment (转挂) API against a running
# instance (default http://localhost:8000). Mirrors the /docs walkthrough:
# a report filed on the wrong patient is moved to the correct one; a shared
# observation is copied, an unshared one keeps its id.
# Usage: BASE_URL=http://localhost:8000 ./scripts/demo_reassign.sh
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
KEY="REASSIGN-DEMO-$(date +%s)"
WRONG_URN="urn:uuid:aaaaaaaa-0000-4000-8000-0000000000a1"
RIGHT_URN="urn:uuid:aaaaaaaa-0000-4000-8000-0000000000a2"
OBS1_URN="urn:uuid:aaaaaaaa-0000-4000-8000-0000000000b1"
OBS2_URN="urn:uuid:aaaaaaaa-0000-4000-8000-0000000000b2"
REPORT_URN="urn:uuid:aaaaaaaa-0000-4000-8000-0000000000c1"
REPORT2_URN="urn:uuid:aaaaaaaa-0000-4000-8000-0000000000c2"

json() { python3 -m json.tool; }
field() { python3 -c "import json,sys;print(json.load(sys.stdin)$1)"; }

echo "1) create wrong + correct patients, and a report (2 observations) on the wrong one"
BUNDLE=$(cat <<JSON
{"resourceType":"Bundle","type":"transaction","entry":[
 {"fullUrl":"$REPORT_URN","resource":{"resourceType":"DiagnosticReport","status":"final",
   "code":{"coding":[{"code":"24323-8"}]},"subject":{"reference":"$WRONG_URN"},
   "result":[{"reference":"$OBS1_URN"},{"reference":"$OBS2_URN"}]},
  "request":{"method":"POST","url":"DiagnosticReport"}},
 {"fullUrl":"$OBS1_URN","resource":{"resourceType":"Observation","status":"final",
   "code":{"coding":[{"code":"2345-7"}]},"subject":{"reference":"$WRONG_URN"},
   "valueQuantity":{"value":5.5,"unit":"mmol/L"}},
  "request":{"method":"POST","url":"Observation"}},
 {"fullUrl":"$OBS2_URN","resource":{"resourceType":"Observation","status":"final",
   "code":{"coding":[{"code":"718-7"}]},"subject":{"reference":"$WRONG_URN"},
   "valueQuantity":{"value":13.2,"unit":"g/dL"}},
  "request":{"method":"POST","url":"Observation"}},
 {"fullUrl":"$WRONG_URN","resource":{"resourceType":"Patient",
   "identifier":[{"system":"http://hospital/mrn","value":"$KEY-WRONG"}]},
  "request":{"method":"POST","url":"Patient"}},
 {"fullUrl":"$RIGHT_URN","resource":{"resourceType":"Patient",
   "identifier":[{"system":"http://hospital/mrn","value":"$KEY-RIGHT"}]},
  "request":{"method":"POST","url":"Patient"}}
]}
JSON
)
RECEIPT=$(curl -fsS -X POST "$BASE_URL/fhir" -H "Idempotency-Key: $KEY" \
  -H "Content-Type: application/json" -d "$BUNDLE")
RID=$(echo "$RECEIPT" | field "['resourceMap'][0]['id']")
OBS1=$(echo "$RECEIPT" | field "['resourceMap'][1]['id']")
OBS2=$(echo "$RECEIPT" | field "['resourceMap'][2]['id']")
WRONG_PID=$(echo "$RECEIPT" | field "['resourceMap'][3]['id']")
RIGHT_PID=$(echo "$RECEIPT" | field "['resourceMap'][4]['id']")
echo "   report=$RID obs1=$OBS1 obs2=$OBS2"
echo "   wrongPatient=$WRONG_PID rightPatient=$RIGHT_PID"

echo "2) a second report on the wrong patient also references obs1 (shared)"
SECOND=$(cat <<JSON
{"resourceType":"Bundle","type":"transaction","entry":[
 {"fullUrl":"$REPORT2_URN","resource":{"resourceType":"DiagnosticReport","status":"final",
   "code":{"coding":[{"code":"24323-8"}]},"subject":{"reference":"Patient/$WRONG_PID"},
   "result":[{"reference":"Observation/$OBS1"}]},
  "request":{"method":"POST","url":"DiagnosticReport"}}
]}
JSON
)
curl -fsS -X POST "$BASE_URL/fhir" -H "Idempotency-Key: $KEY-2" \
  -H "Content-Type: application/json" -d "$SECOND" > /dev/null
echo "   second report created, references obs1=$OBS1"

echo "3) stale version is rejected (412)"
curl -sS -o /dev/null -w "   HTTP %{http_code}\n" -X POST \
  "$BASE_URL/diagnostic-reports/$RID/reassign" \
  -H "Content-Type: application/json" \
  -d "{\"expectedVersion\": 99, \"targetPatientId\": \"$RIGHT_PID\"}"

echo "4) reassign the report to the correct patient"
RECEIPT2=$(curl -fsS -X POST "$BASE_URL/diagnostic-reports/$RID/reassign" \
  -H "Content-Type: application/json" \
  -d "{\"expectedVersion\": 1, \"targetPatientId\": \"$RIGHT_PID\"}")
echo "$RECEIPT2" | json
COPY_ID=$(echo "$RECEIPT2" | field "['observations'][0]['afterId']")

echo "5) verify: shared obs1 was copied ($OBS1 -> $COPY_ID), obs2 kept its id"
curl -fsS "$BASE_URL/diagnostic-reports/$RID" | json
echo "6) history remains queryable (report versions 1 and 2)"
curl -fsS "$BASE_URL/diagnostic-reports/$RID/_history" | python3 -c \
  "import json,sys; h=json.load(sys.stdin); print('   versions:', [e['version'] for e in h['entry']])"
echo "REASSIGN DEMO OK"
