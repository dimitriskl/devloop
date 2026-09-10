import json
import pathlib
import re
import sys
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")
spec = json.load(sys.stdin)
operation = sys.argv[1]
allowed = {"PublicQueryInfo", "PublicQueryLayout", "FetchOdsTableInfo", "SimpleScroller", "VoucherProfileInfo", "VoucherStateInfo", "VoucherStates", "VoucherProfileRelations"}
if operation not in allowed:
    raise SystemExit("Only the two approved read-only metadata operations are supported")
base = spec["BaseUrl"].rstrip("/")
if base != "https://api.entersoft.gr/api/rpc":
    raise SystemExit("Unexpected configured EBS host or API path")
metadata_tables = {"FetchOdsTableInfo": "ESFIVoucher", "VoucherProfileInfo": "ESFIVoucherPromotionProfile", "VoucherStateInfo": "ESFIZVoucherState"}
route = metadata_tables.get(operation, "ESFIVoucher/ESFIVoucher_Def")
endpoint = "FetchOdsTableInfo" if operation in metadata_tables else operation
if operation == "VoucherStates":
    endpoint, route = "FetchStdZoom", "ESFIZVoucherState"
if operation == "VoucherProfileRelations":
    endpoint, route = "FetchOdsMasterRelationsInfo", "ESFIVoucherPromotionProfile"
url = base + "/" + endpoint + "/" + route

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

request = urllib.request.Request(url, headers={spec["TokenHeaderName"]: spec["Key"], "Accept": "application/json"}, method="GET")
result = {"method": "GET", "url": url}
try:
    with urllib.request.build_opener(NoRedirect).open(request, timeout=20) as response:
        raw = response.read(2_000_001)
        result["httpStatus"] = response.status
except urllib.error.HTTPError as error:
    result["httpStatus"] = error.code
    raw = error.read(20_000)
except Exception as error:
    result["errorType"] = type(error).__name__
    raw = str(error).encode()

text = raw.decode("utf-8-sig", errors="replace").replace(spec["Key"], "[REDACTED]")
if len(raw) > 2_000_000:
    result["error"] = "Response exceeded the metadata size bound"
else:
    try:
        result["response"] = json.loads(text)
    except ValueError:
        result["responseText"] = text[:2000]
if operation == "SimpleScroller" and result.get("httpStatus") == 200:
    safe_fields = {"Type", "VoucherType", "DiscVoucherValue", "DiscVoucherPercentage", "Inactive", "Printed", "RegistrationDate", "ExpirationDate", "ESDCreated", "ESDModified", "fVoucherStateCode", "VoucherState", "PromotionProfileCode", "fCompanyCode"}
    summaries = []
    def summarize(value, path="root"):
        if isinstance(value, list) and value and isinstance(value[0], dict):
            columns = sorted({k for row in value if isinstance(row, dict) for k in row})
            sample = [{k: v for k, v in row.items() if k in safe_fields} for row in value[:5]]
            summaries.append({"path": path, "rowsReceived": len(value), "columns": columns, "sample": sample})
        elif isinstance(value, dict):
            for k, v in value.items():
                if isinstance(v, (list, dict)):
                    summarize(v, path + "." + k)
    parsed = result.pop("response", None)
    if parsed is None and len(raw) > 2_000_000:
        decoder = json.JSONDecoder()
        array = re.search(r'"([^"\\]+)"\s*:\s*\[', text)
        start = array.end() if array else (1 if text.lstrip().startswith("[") else None)
        if start is not None:
            rows = []
            while len(rows) < 50:
                while start < len(text) and text[start] in " \r\n\t,":
                    start += 1
                try:
                    row, end = decoder.raw_decode(text, start)
                except ValueError:
                    break
                if not isinstance(row, dict):
                    break
                rows.append(row)
                start = end
            parsed = {array.group(1) if array else "rows": rows}
            result["sampleOnly"] = True
            result["samplingLimit"] = 50
            result.pop("error", None)
    summarize(parsed)
    result["rowSummaries"] = summaries
out = pathlib.Path(__file__).resolve().parents[2] / "output" / "coupon-analysis"
out.mkdir(parents=True, exist_ok=True)
(out / (operation + ".json")).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
display = result.copy()
if operation in metadata_tables and isinstance(result.get("response"), dict):
    response = result["response"]
    display["response"] = {"ID": response.get("ID"), "columns": [{k: c.get(k) for k in ("ID", "ODSType", "NetType", "Nullable", "ChoiceType", "Size", "HelpTxt")} for c in response.get("Columns", [])]}
print(json.dumps(display, ensure_ascii=False))
