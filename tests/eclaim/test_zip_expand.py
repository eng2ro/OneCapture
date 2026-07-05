"""Unit tests for the ZIP-of-receipts expansion (no DB, pure function).

A ZIP upload on the capture page is a batch of receipts — one image per line, as
if the files had been dropped individually. ``_expand_zip`` unpacks it, bounded
against zip bombs and hostile entries.
"""

from __future__ import annotations

import io
import zipfile

from eclaim.services.ingestion import (
    _EXT_MEDIA,
    _ZIP_MAX_ENTRIES,
    expand_zip as _expand_zip,
    is_zip as _is_zip,
)


def _zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_is_zip_by_media_type_or_name():
    assert _is_zip("receipts.zip", "application/octet-stream")
    assert _is_zip("x", "application/zip")
    assert not _is_zip("photo.jpg", "image/jpeg")


def test_expands_images_one_per_entry_and_maps_media_type():
    data = _zip({"a.jpg": b"\xff\xd8jpg", "b.PNG": b"\x89PNGdata", "c.webp": b"webp"})
    inputs, errors = _expand_zip("batch.zip", data)
    assert errors == []
    assert [i["media_type"] for i in inputs] == ["image/jpeg", "image/png", "image/webp"]
    # Each entry carries its bytes and no client-side item (server OCRs it).
    assert all(i["item"] is None and i["bytes"] for i in inputs)
    assert {i["name"] for i in inputs} == {"a.jpg", "b.PNG", "c.webp"}


def test_skips_non_images_dirs_hidden_and_macosx():
    data = _zip({
        "notes.txt": b"hello",
        "sub/": b"",
        ".DS_Store": b"junk",
        "__MACOSX/._a.jpg": b"junk",
        "real.jpg": b"\xff\xd8",
    })
    inputs, errors = _expand_zip("mixed.zip", data)
    assert [i["name"] for i in inputs] == ["real.jpg"]


def test_flattens_nested_paths_to_base_name_no_traversal():
    data = _zip({"trip/day1/dinner.jpg": b"\xff\xd8", "../evil.png": b"\x89P"})
    inputs, _ = _expand_zip("nested.zip", data)
    names = {i["name"] for i in inputs}
    assert names == {"dinner.jpg", "evil.png"}   # base name only — no path/traversal


def test_bad_zip_reports_error():
    inputs, errors = _expand_zip("broken.zip", b"not a zip at all")
    assert inputs == []
    assert errors and "readable ZIP" in errors[0]


def test_empty_or_imageless_zip_reports_error():
    data = _zip({"readme.txt": b"nothing here"})
    inputs, errors = _expand_zip("docs.zip", data)
    assert inputs == []
    assert errors and "no receipt images" in errors[0]


def test_entry_count_cap_stops_and_warns():
    entries = {f"r{n}.jpg": b"\xff\xd8" for n in range(_ZIP_MAX_ENTRIES + 5)}
    inputs, errors = _expand_zip("huge.zip", _zip(entries))
    assert len(inputs) == _ZIP_MAX_ENTRIES
    assert any("were skipped" in e for e in errors)


def test_all_supported_image_extensions_map():
    # .heic/.heif included so a ZIP of iPhone photos expands too (each entry is then
    # transcoded to JPEG in the flatten step before OCR/storage).
    assert set(_EXT_MEDIA) == {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}
