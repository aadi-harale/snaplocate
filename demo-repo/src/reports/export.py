"""Export endpoint for the reports screen."""
from .builder import build_monthly

DOWNLOAD_LABEL = "Download CSV"


def handle_export(request):
    try:
        body = build_monthly(request.since, request.until)
    except RuntimeError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "body": body, "filename": "sales.csv"}
