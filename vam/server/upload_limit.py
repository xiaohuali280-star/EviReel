from starlette.responses import JSONResponse
from starlette.formparsers import MultiPartException


class UploadLimitMiddleware:
    """Bound the request body before multipart parsing/spooling, including chunked uploads."""
    def __init__(self, app, max_bytes):
        self.app, self.limit = app, max_bytes + 64 * 1024

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"].rstrip("/") != "/api/videos":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        try:
            length = int(headers.get(b"content-length", b"0"))
        except ValueError:
            return await JSONResponse({"error": {"code": "invalid_length", "message": "Invalid Content-Length"}}, status_code=400)(scope, receive, send)
        if length > self.limit:
            return await JSONResponse({"error": {"code": "upload_limit", "message": "Upload exceeds configured size limit"}}, status_code=413)(scope, receive, send)
        size = 0
        async def bounded_receive():
            nonlocal size
            message = await receive()
            size += len(message.get("body", b""))
            if size > self.limit:
                scope.setdefault("state", {})["upload_limit_exceeded"] = True
                # The multipart parser closes partially spooled files on this exception.
                raise MultiPartException("Upload exceeds configured size limit")
            return message
        return await self.app(scope, bounded_receive, send)
