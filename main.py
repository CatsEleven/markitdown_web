import re
import threading
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx
from docling.document_converter import DocumentConverter
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE = Path(__file__).parent
INPUTS = BASE / "inputs"
OUTPUTS = BASE / "outputs"
INPUTS.mkdir(exist_ok=True)
OUTPUTS.mkdir(exist_ok=True)

app = FastAPI(title="PDF → Markdown (docling)")

_converter: DocumentConverter | None = None
_lock = threading.Lock()


def convert_pdf(pdf: Path) -> str:
    """docling で変換し outputs/<stem>.md に保存。重いので同時実行は直列化する。"""
    global _converter
    with _lock:
        if _converter is None:
            _converter = DocumentConverter()
        result = _converter.convert(pdf)
        md = result.document.export_to_markdown()
    (OUTPUTS / f"{pdf.stem}.md").write_text(md, encoding="utf-8")
    return md


def safe_name(name: str) -> str:
    name = Path(unquote(name)).name
    name = re.sub(r"[\x00-\x1f/\\]", "_", name).strip(" .")
    if not name:
        name = "document.pdf"
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return name


def check_pdf(path: Path) -> None:
    with path.open("rb") as f:
        if f.read(5) != b"%PDF-":
            path.unlink(missing_ok=True)
            raise HTTPException(400, "PDFファイルではありません")


def response(pdf: Path, md: str, cached: bool = False) -> dict:
    return {"name": pdf.name, "stem": pdf.stem, "markdown": md, "cached": cached}


@app.get("/api/files")
def list_files():
    items = []
    for pdf in sorted(INPUTS.glob("*.pdf")):
        items.append({"name": pdf.name, "converted": (OUTPUTS / f"{pdf.stem}.md").exists()})
    return items


@app.delete("/api/files/{name}")
def delete_file(name: str):
    pdf = INPUTS / safe_name(name)
    if not pdf.is_file():
        raise HTTPException(404, "PDFが見つかりません")
    pdf.unlink()
    (OUTPUTS / f"{pdf.stem}.md").unlink(missing_ok=True)
    return {"deleted": pdf.name}


@app.post("/api/convert")
def convert_upload(file: UploadFile = File(...)):
    dest = INPUTS / safe_name(file.filename or "document.pdf")
    with dest.open("wb") as f:
        while chunk := file.file.read(1 << 20):
            f.write(chunk)
    check_pdf(dest)
    return response(dest, convert_pdf(dest))


class UrlBody(BaseModel):
    url: str


@app.post("/api/convert-url")
def convert_url(body: UrlBody):
    parsed = urlparse(body.url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(400, "http(s) のURLを指定してください")
    try:
        with httpx.stream("GET", body.url.strip(), follow_redirects=True, timeout=60) as r:
            r.raise_for_status()
            name = safe_name(Path(urlparse(str(r.url)).path).name or "document.pdf")
            dest = INPUTS / name
            with dest.open("wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    f.write(chunk)
    except httpx.HTTPError as e:
        raise HTTPException(400, f"ダウンロードに失敗しました: {e}")
    check_pdf(dest)
    return response(dest, convert_pdf(dest))


@app.post("/api/convert/{name}")
def convert_existing(name: str, force: bool = False):
    """inputs 内の既存PDFを開く。変換済みなら outputs の md を返す。"""
    pdf = INPUTS / safe_name(name)
    if not pdf.is_file():
        raise HTTPException(404, "PDFが見つかりません")
    md_path = OUTPUTS / f"{pdf.stem}.md"
    if md_path.exists() and not force:
        return response(pdf, md_path.read_text(encoding="utf-8"), cached=True)
    return response(pdf, convert_pdf(pdf))


@app.get("/api/pdf/{name}")
def get_pdf(name: str):
    pdf = INPUTS / safe_name(name)
    if not pdf.is_file():
        raise HTTPException(404)
    return FileResponse(pdf, media_type="application/pdf")


@app.get("/api/markdown/{name}", response_class=PlainTextResponse)
def get_markdown(name: str):
    md = OUTPUTS / f"{Path(safe_name(name)).stem}.md"
    if not md.is_file():
        raise HTTPException(404)
    return md.read_text(encoding="utf-8")


app.mount("/", StaticFiles(directory=BASE / "static", html=True), name="static")


def main():
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
