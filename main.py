import asyncio
import os
import shutil
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from starlette.responses import JSONResponse

ROOT = Path(__file__).resolve().parent.parent
TEMP = Path(os.getenv("TEMP_DIR", str(ROOT / "tmp"))).resolve()
MAX_BYTES = int(os.getenv("MAX_UPLOAD_MB", "50")) * 1024 * 1024
TIMEOUT = int(os.getenv("CONVERT_TIMEOUT_SECONDS", "180"))
TTL = int(os.getenv("TEMP_TTL_SECONDS", "3600"))
active: set[Path] = set()
busy = False
EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}


def remove_job(path: Path):
    shutil.rmtree(path, ignore_errors=True)
    active.discard(path)


def sweep():
    for path in TEMP.iterdir():
        if (len(path.name) == 32 and all(c in "0123456789abcdef" for c in path.name)
                and path.is_dir() and path not in active and time.time() - path.stat().st_mtime > TTL):
            shutil.rmtree(path, ignore_errors=True)


class UploadGuard:
    """限制完整 multipart 請求，避免檔案解析前耗盡暫存空間。單 worker。"""
    def __init__(self, app):
        self.app = app
        self.occupied = False

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] != "/api/convert" or scope["method"] != "POST":
            return await self.app(scope, receive, send)
        if self.occupied:
            return await JSONResponse({"detail": "服務忙碌，請稍後再試。"}, 429)(scope, receive, send)
        headers = dict(scope.get("headers", []))
        try:
            length = int(headers.get(b"content-length", b"0"))
        except ValueError:
            return await JSONResponse({"detail": "無效的 Content-Length。"}, 400)(scope, receive, send)
        if length > MAX_BYTES + 1024 * 1024:
            return await JSONResponse({"detail": "上傳請求超過大小限制。"}, 413)(scope, receive, send)
        self.occupied = True
        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            received += len(message.get("body", b""))
            if received > MAX_BYTES + 1024 * 1024:
                raise HTTPException(413, "上傳請求超過大小限制。")
            return message

        try:
            await self.app(scope, limited_receive, send)
        finally:
            self.occupied = False


class TemporaryAudioResponse(FileResponse):
    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            remove_job(Path(self.path).parent)


async def janitor():
    while True:
        sweep()
        await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(app):
    if not shutil.which("ffmpeg"):
        raise RuntimeError("FFmpeg 未安裝，請先安裝 FFmpeg 或使用 Docker。")
    TEMP.mkdir(parents=True, exist_ok=True)
    sweep()
    task = asyncio.create_task(janitor())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="影片轉 MP3", lifespan=lifespan)
app.add_middleware(UploadGuard)
origins = [s.strip() for s in os.getenv("CORS_ORIGINS", "").split(",") if s.strip()]
app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=False,
                   allow_methods=["GET", "POST"], allow_headers=["Content-Type"],
                   expose_headers=["Content-Disposition"])


@app.get("/")
async def home():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/health")
async def health():
    return {"status": "ok", "max_upload_mb": MAX_BYTES // (1024 * 1024)}


@app.post("/api/convert")
async def convert(file: UploadFile = File(...)):
    global busy
    job = None
    proc = None
    delivered = False
    acquired = False
    try:
        if busy:
            raise HTTPException(429, "目前有影片正在轉換，請稍後再試。")
        busy = acquired = True
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in EXTENSIONS:
            raise HTTPException(415, "請上傳 MP4、MOV、MKV、WEBM、AVI 或 M4V 影片。")
        job = TEMP / uuid.uuid4().hex
        job.mkdir()
        active.add(job)
        source, output = job / ("input" + suffix), job / "audio.mp3"
        size = 0
        with source.open("wb") as dest:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_BYTES:
                    raise HTTPException(413, f"影片不可超過 {MAX_BYTES // (1024 * 1024)} MB。")
                dest.write(chunk)
        if size == 0:
            raise HTTPException(400, "影片檔案是空的。")
        # 不使用 shell；限制輸入協議，避免媒體檔引用遠端資源。
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-protocol_whitelist", "file,pipe", "-threads", "1", "-i", str(source),
            "-map", "0:a:0", "-vn", "-t", "3600", "-c:a", "libmp3lame",
            "-b:a", "192k", "-threads", "1", "-y", str(output),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        try:
            await asyncio.wait_for(proc.wait(), TIMEOUT)
        except asyncio.TimeoutError:
            raise HTTPException(408, "轉換逾時，請使用較短的影片。")
        if proc.returncode != 0 or not output.exists() or output.stat().st_size == 0:
            raise HTTPException(422, "無法提取音頻：影片可能損壞、格式不支援或沒有音軌。")
        source.unlink()
        response = TemporaryAudioResponse(output, media_type="audio/mpeg", filename="audio.mp3")
        delivered = True
        return response
    finally:
        if proc is not None and proc.returncode is None:
            proc.kill()
            await proc.wait()
        await file.close()
        if job and not delivered:
            remove_job(job)
        if acquired:
            busy = False
