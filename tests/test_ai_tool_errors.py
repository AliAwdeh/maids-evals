import os


def test_ai_tool_error_does_not_return_cloudflare_502():
    os.environ.setdefault("SESSION_SECRET", "test-session-secret-for-ai-errors")
    os.environ.setdefault("ADMIN_USERNAME", "mapuser")
    os.environ.setdefault("ADMIN_TOKEN", "map-token")
    from app import AI_TOOL_ERROR_STATUS, ai_tool_error

    html = """
    <!DOCTYPE html>
    <html><head><title>maidscc.app | 502: Bad gateway</title></head>
    <body>Cloudflare host error</body></html>
    """
    resp = ai_tool_error("Could not study the sample", RuntimeError(html))
    assert resp.status_code == AI_TOOL_ERROR_STATUS
    assert resp.status_code != 502
    assert b"Could not study the sample: maidscc.app | 502: Bad gateway" in resp.body
    assert b"<!DOCTYPE html>" not in resp.body
