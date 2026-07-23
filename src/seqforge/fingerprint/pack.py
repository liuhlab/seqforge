"""Information files (paper/spreadsheet) and the deterministic tar.gz that carries the package.

The reads answer *what the library is*; the prose answers *what the sample was* and *what to do with
it*. A fingerprint that dropped the paper would still resolve the chemistry but could not reproduce the
harvested assertions, so ``preflight`` carries the information files too — the original document (so a
fingerprint run harvests byte-identically), its extracted text, and, for a PDF, its embedded images.

The tar is written deterministically — entries sorted, ``mtime`` zeroed, ownership and permissions
fixed — so ``preflight`` run twice over the same inputs yields a byte-identical package. Combined with
the ``mtime=0`` gzip idiom the reads use, the whole ``.fingerprint.tar.gz`` is content-addressable.
"""

from __future__ import annotations

import gzip
import shutil
import tarfile
from pathlib import Path


def _write_text(dest: Path, text: str) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")


def extract_pdf_images(pdf: Path, outdir: Path) -> list[str]:
    """Extract a PDF's embedded raster images as PNGs under ``outdir``. Best-effort, never raises.

    Uses PyMuPDF's ``page.get_images`` — the same engine ``harvest`` already reads text with — and
    normalises CMYK/alpha to RGB. Deterministic filenames (``pNNN-iMM.png``) so the package is
    reproducible; a page with no images contributes nothing. Returns package-relative paths.
    """
    try:
        import pymupdf
    except ImportError:  # pragma: no cover - pymupdf is a hard dependency, but degrade gracefully
        return []
    out: list[str] = []
    try:
        doc = pymupdf.open(str(pdf))
    except Exception:  # noqa: BLE001 - a malformed PDF must not sink the whole package
        return []
    try:
        for pno in range(doc.page_count):
            page = doc.load_page(pno)
            for idx, img in enumerate(page.get_images(full=True)):
                xref = img[0]
                try:
                    pix = pymupdf.Pixmap(doc, xref)
                    if pix.n - pix.alpha >= 4:  # CMYK (or CMYK+alpha) -> RGB for a portable PNG
                        pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
                    name = f"p{pno + 1:03d}-i{idx + 1:02d}.png"
                    outdir.mkdir(parents=True, exist_ok=True)
                    pix.save(str(outdir / name))
                except Exception:  # noqa: BLE001 - skip a single unreadable image, keep the rest
                    continue
                out.append(f"{outdir.name}/{name}")
    finally:
        doc.close()
    return out


def extract_info(docs: list[Path], staging: Path, *, include_raw: bool = True) -> list[str]:
    """Carry every information document into the package: extracted text, and (locally) the original.

    Returns the sorted package-relative paths written under ``info/``. Two modes, one knob:

    - ``include_raw=True`` (default, a **local** package): the original is copied verbatim into
      ``info/docs/`` so a fingerprint run's ``harvest`` reads the identical bytes (and so reproduces
      the identical span-verified assertions), a PDF's embedded images are extracted into
      ``info/images/``, and ``info/text/`` carries the extracted text as a convenience.
    - ``include_raw=False`` (a **redistributable** package): only ``info/text/`` is written. The raw
      paper is NOT redistributed (a copyright constraint) and figures are dropped too (the figure
      pipeline is not good enough yet — text is what ``harvest`` needs). A fingerprint run falls back
      to the extracted text (:meth:`LoadedFingerprint.info_paths`), so the package stays usable; the
      only cost is that a ``.txt`` doc has a different ``doc_sha256`` and no PDF per-page offsets, so
      the harvested assertions are *equivalent*, not byte-identical, to a raw-PDF run.

    A document that cannot be read for text still gets copied when ``include_raw`` — the original is
    the authority, the text is a convenience. A redistributable package simply omits an unreadable doc.
    """
    info: list[str] = []
    info_root = staging / "info"
    for doc in docs:
        doc = Path(doc)
        if include_raw:
            docs_dir = info_root / "docs"
            docs_dir.mkdir(parents=True, exist_ok=True)
            # copyfile, not copy2: mtime is the tar's to zero
            shutil.copyfile(doc, docs_dir / doc.name)
            info.append(f"info/docs/{doc.name}")

        try:
            from ..harvest.normalize import read_document

            text = read_document(doc)
        except Exception:  # noqa: BLE001 - a document we cannot parse is not a reason to fail preflight
            text = ""
        if text.strip():
            _write_text(info_root / "text" / f"{doc.stem}.txt", text)
            info.append(f"info/text/{doc.stem}.txt")

        if include_raw and doc.suffix.lower() == ".pdf":
            imgs = extract_pdf_images(doc, info_root / "images" / doc.stem)
            info.extend(f"info/images/{doc.stem}/{Path(rel).name}" for rel in imgs)

    return sorted(set(info))


def write_tar_gz(src_dir: Path, dest: Path) -> None:
    """Pack ``src_dir`` into a REPRODUCIBLE ``dest`` (.tar.gz): sorted, ``mtime=0``, fixed ownership.

    Nothing wall-clock- or host-dependent enters the archive, so two runs over identical staged bytes
    produce byte-identical output — the property that makes the whole package content-addressable.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    entries = sorted(src_dir.rglob("*"), key=lambda p: str(p.relative_to(src_dir)))
    with open(dest, "wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w") as tar:
            for path in entries:
                arcname = str(path.relative_to(src_dir))
                info = tar.gettarinfo(str(path), arcname=arcname)
                info.mtime = 0
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                info.mode = 0o755 if path.is_dir() else 0o644
                if path.is_file():
                    with open(path, "rb") as fh:
                        tar.addfile(info, fh)
                else:
                    tar.addfile(info)


__all__ = ["extract_info", "extract_pdf_images", "write_tar_gz"]
