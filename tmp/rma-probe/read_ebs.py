import datetime
import json
import pathlib
import sys
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")
operation = sys.argv[1]
out = pathlib.Path(__file__).resolve().parents[2] / "output" / "rma-analysis-20260909"
out.mkdir(parents=True, exist_ok=True)
routes = {
    "PublicQueryInfo": "PublicQueryInfo/WEB_Scrolls/fnky_EshopRMA",
    "PublicQueryLayout": "PublicQueryLayout/WEB_Scrolls/fnky_EshopRMA",
    "SimpleScroller": "SimpleScroller/WEB_Scrolls/fnky_EshopRMA",
    "StagingTableInfo": "FetchOdsTableInfo/CSFunkyIncomingRMA",
}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


if operation == "Swagger":
    url = "https://eswebapi-next.azurewebsites.net/swagger/docs/api3.0"
    with urllib.request.build_opener(NoRedirect).open(url, timeout=20) as response:
        raw = response.read(5_000_001)
    if len(raw) > 5_000_000:
        raise SystemExit("Swagger size limit exceeded")
    doc = json.loads(raw)
    selected = {p: v for p, v in doc["paths"].items() if any(s in p for s in ("Scroller", "PublicQuery", "FetchOdsTableInfo"))}
    definitions = {k: v for k, v in doc["definitions"].items() if any(s in k for s in ("ScrollerCommand", "ESPQLayout", "ESOdsTableInfo", "ESOdsColumnInfo"))}
    evidence = {"source": url, "retrievedUtc": datetime.datetime.now(datetime.timezone.utc).isoformat(), "paths": selected, "definitions": definitions}
    (out / "Swagger.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"saved": "Swagger.json", "definitions": definitions}, ensure_ascii=False))
    raise SystemExit(0)

if operation not in routes:
    raise SystemExit("Unsupported read operation")
spec = json.load(sys.stdin)
if spec["BaseUrl"].rstrip("/") != "https://api.entersoft.gr/api/rpc":
    raise SystemExit("Configured host/path changed")
if spec["TokenHeaderName"] != "X-ESAPIKEY-ECOMCONNECTOR" or not spec["Key"]:
    raise SystemExit("Authentication contract changed")
url = spec["BaseUrl"].rstrip("/") + "/" + routes[operation]
result = {"method": "GET", "url": url, "retrievedUtc": datetime.datetime.now(datetime.timezone.utc).isoformat()}
request = urllib.request.Request(url, headers={spec["TokenHeaderName"]: spec["Key"], "Accept": "application/json"}, method="GET")
try:
    with urllib.request.build_opener(NoRedirect).open(request, timeout=20) as response:
        raw = response.read(2_000_001)
        result["httpStatus"] = response.status
except urllib.error.HTTPError as error:
    result["httpStatus"] = error.code
    raw = error.read(20_000)
except Exception as error:
    result["errorType"] = type(error).__name__
    raw = b""

result["responseBytesRead"] = len(raw)
result["responseTruncated"] = len(raw) > 2_000_000
body = raw.decode("utf-8-sig", errors="replace").replace(spec["Key"], "[REDACTED]")
try:
    parsed = json.loads(body) if not result["responseTruncated"] else None
except ValueError:
    parsed = None

if operation == "SimpleScroller" and result.get("httpStatus") == 200:
    summaries = []

    def summarize(value, path="root"):
        if isinstance(value, list):
            rows = [r for r in value if isinstance(r, dict)]
            columns = sorted({k for row in rows for k in row})
            summaries.append({"path": path, "rowsReceived": len(value), "columns": [{"name": k, "observedJsonTypes": sorted({type(row[k]).__name__ for row in rows if k in row}), "nullCount": sum(row.get(k) is None for row in rows), "emptyStringCount": sum(row.get(k) == "" for row in rows)} for k in columns]})
        elif isinstance(value, dict):
            for key, item in value.items():
                if isinstance(item, (dict, list)):
                    summarize(item, path + "." + key)

    summarize(parsed)
    result["rowSummaries"] = summaries
    result["rowValuesRetained"] = False
elif parsed is not None:
    result["response"] = parsed
else:
    result["parseableJson"] = False

(out / (operation + ".json")).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(result, ensure_ascii=False))
