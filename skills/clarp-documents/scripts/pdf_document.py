#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["weasyprint>=66,<67", "Markdown>=3.8,<4", "PyMuPDF>=1.26,<2"]
# ///
"""Render local Markdown/HTML to searchable PDF, inspect pages, publish a file artifact."""
from __future__ import annotations
import argparse
import hashlib
import html
import json
from pathlib import Path
import subprocess
import sys
from urllib.parse import unquote, urlparse


def local_fetcher(root: Path, errors: list[str] | None = None):
    from weasyprint import default_url_fetcher
    def fetch(url, *args, **kwargs):
        try:
            return allowed(url, *args, **kwargs)
        except Exception as error:
            if errors is not None:
                errors.append(str(error))
            raise
    def allowed(url, *args, **kwargs):
        parsed = urlparse(url)
        if parsed.scheme == "data":
            return default_url_fetcher(url, *args, **kwargs)
        if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
            raise ValueError("PDF assets must be local; download authorized assets first")
        path = Path(unquote(parsed.path)).resolve()
        if not path.is_relative_to(root):
            raise ValueError("PDF asset is outside the input directory")
        return default_url_fetcher(url, *args, **kwargs)
    return fetch


def inspect(path: Path, output: Path) -> dict:
    import pymupdf
    output.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open(path)
    if doc.is_encrypted or not doc.page_count:
        raise ValueError("PDF must be readable and contain pages")
    pages = []
    for number, page in enumerate(doc, 1):
        text = page.get_text()
        outside = [list(block[:4]) for block in page.get_text("blocks")
                   if not page.rect.contains(pymupdf.Rect(block[:4]))]
        image = output / f"page-{number:03d}.png"
        page.get_pixmap(matrix=pymupdf.Matrix(1.2, 1.2), alpha=False).save(image)
        pages.append({"page": number, "characters": len(text.strip()),
                      "links": len(page.get_links()), "outside_page": outside, "image": str(image)})
    if sum(p["characters"] for p in pages) < 40:
        raise ValueError("PDF has no meaningful searchable text; do not publish an image-only document")
    report = {"pdf": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
              "page_count": len(pages), "pages": pages,
              "visual_review_required": True}
    (output / "inspection.json").write_text(json.dumps(report, indent=2) + "\n")
    (output / "text.txt").write_text("\n\f\n".join(page.get_text() for page in doc))
    if any(p["outside_page"] for p in pages):
        raise ValueError("Text extends beyond a PDF page; see inspection.json")
    return report


def render(args):
    import markdown
    from weasyprint import CSS, HTML
    source = args.source.resolve()
    body = source.read_text()
    if source.suffix.lower() in {".md", ".markdown"}:
        body = markdown.markdown(body, extensions=["tables", "fenced_code", "toc", "sane_lists"])
    elif source.suffix.lower() not in {".html", ".htm"}:
        raise ValueError("Input must be Markdown or HTML")
    if args.preface:
        body = args.preface.read_text() + '<div class="page-break"></div>' + body
    document = '<!doctype html><html lang="en"><head><meta charset="utf-8"><title>' + html.escape(args.title) + '</title></head><body data-title="' + html.escape(args.title, quote=True) + '">' + body + '</body></html>'
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists() and not args.force:
        raise ValueError("Output exists; use --force only to replace your generated PDF")
    stylesheet = Path(__file__).parent.parent / "templates/document.css"
    resource_errors: list[str] = []
    fetcher = local_fetcher(source.parent, resource_errors)
    styles = [CSS(filename=str(stylesheet))]
    if args.css:
        styles.append(CSS(filename=str(args.css), url_fetcher=fetcher))
    HTML(string=document, base_url=source.parent.as_uri() + "/", url_fetcher=fetcher).write_pdf(args.output, stylesheets=styles)
    if resource_errors:
        raise ValueError("PDF has missing or disallowed assets: " + "; ".join(resource_errors))
    return inspect(args.output.resolve(), args.output.with_suffix(".pages").resolve())


def publish(args):
    import pymupdf
    path = args.pdf.resolve()
    with pymupdf.open(path) as doc:
        if doc.is_encrypted or sum(len(p.get_text().strip()) for p in doc) < 40:
            raise ValueError("Publish a readable PDF with searchable text")
    receipt = args.receipt.resolve()
    # Reserve a receipt before the first mutation. Never repeat an ambiguous POST.
    with receipt.open("x") as stream:
        json.dump({"status": "started", "pdf": str(path), "session": args.session}, stream)
    uploaded = json.loads(subprocess.check_output(
        ["clarp-media-publish", "--session", args.session, "--json", str(path)], text=True))
    receipt.write_text(json.dumps({"status": "uploaded", "upload": uploaded}, indent=2) + "\n")
    asset = uploaded.get("asset", uploaded)
    payload = {"url": asset["url"], "mime_type": "application/pdf", "file_name": path.name,
               "size_bytes": path.stat().st_size}
    artifact = json.loads(subprocess.check_output(
        ["clarp-agent-artifacts", "create", args.session, "file", args.title, args.summary,
         json.dumps(payload)], text=True))
    result = {"status": "published", "artifact": artifact, "upload": uploaded}
    receipt.write_text(json.dumps(result, indent=2) + "\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    renderer = commands.add_parser("render")
    renderer.add_argument("source", type=Path)
    renderer.add_argument("--output", required=True, type=Path)
    renderer.add_argument("--title", required=True)
    renderer.add_argument("--preface", type=Path, help="Local HTML visual plates prepended to the complete source")
    renderer.add_argument("--css", type=Path)
    renderer.add_argument("--force", action="store_true")
    inspector = commands.add_parser("inspect")
    inspector.add_argument("pdf", type=Path)
    inspector.add_argument("--output", required=True, type=Path)
    publisher = commands.add_parser("publish")
    publisher.add_argument("pdf", type=Path)
    publisher.add_argument("--session", required=True)
    publisher.add_argument("--title", required=True)
    publisher.add_argument("--summary", default="")
    publisher.add_argument("--receipt", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = render(args) if args.command == "render" else (inspect(args.pdf.resolve(), args.output.resolve()) if args.command == "inspect" else publish(args))
        print(json.dumps(result, indent=2))
    except Exception as error:
        print(f"pdf_document: {error}", file=sys.stderr)
        return 1
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
