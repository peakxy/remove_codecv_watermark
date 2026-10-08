#!/usr/bin/env python3
"""Remove CodeCV's tiled PDF watermark from exported resumes."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
from typing import Iterable

from pypdf import PdfReader, PdfWriter
from pypdf.generic import ContentStream, DecodedStreamObject, DictionaryObject, NameObject


DEFAULT_OUTPUT_SUFFIX = ".clean.pdf"

# Verified CodeCV transparent overlay, decoded pixels (not compressed bytes).
# Match both the artwork and alpha mask so ordinary resume images stay intact.
CODECV_IMAGE_SIZE = (1588, 2246)
CODECV_IMAGE_SHA256 = "ff3f22fd3eeccce53c94b6fe0cf00c59530452eccbfcfc173af585aaa3c40350"
CODECV_MASK_SHA256 = "8bc017c2b9656ee24f3438b304f73aaf6a372857fa6c2d6cf8077945bf87a8b0"


def _is_codecv_image(image) -> bool:
    image = image.get_object()
    if (
        image.get("/Subtype") != "/Image"
        or (image.get("/Width"), image.get("/Height")) != CODECV_IMAGE_SIZE
        or image.get("/ColorSpace") != "/DeviceRGB"
        or image.get("/BitsPerComponent") != 8
        or "/SMask" not in image
    ):
        return False
    mask = image["/SMask"]
    if (
        mask.get("/Subtype") != "/Image"
        or (mask.get("/Width"), mask.get("/Height")) != CODECV_IMAGE_SIZE
        or mask.get("/ColorSpace") != "/DeviceGray"
        or mask.get("/BitsPerComponent") != 8
    ):
        return False
    return (
        hashlib.sha256(image.get_data()).hexdigest() == CODECV_IMAGE_SHA256
        and hashlib.sha256(mask.get_data()).hexdigest() == CODECV_MASK_SHA256
    )


def _is_name(value, expected: str) -> bool:
    return isinstance(value, NameObject) and str(value) == expected


def _first_name(operands: Iterable[object]) -> str | None:
    for operand in operands:
        if isinstance(operand, NameObject):
            return str(operand)
    return None


def _is_pattern_fill(operations, index: int) -> tuple[int, str | None]:
    """Return the operation count for a CodeCV watermark fill, or zero."""
    if index + 5 >= len(operations):
        return 0, None

    op0, op1, op2, op3 = operations[index : index + 4]
    uses_pattern_space = (
        op0[1] == b"CS"
        and op1[1] == b"cs"
        and len(op0[0]) == 1
        and len(op1[0]) == 1
        and _is_name(op0[0][0], "/Pattern")
        and _is_name(op1[0][0], "/Pattern")
    )
    if not uses_pattern_space or op2[1] != b"SCN" or op3[1] != b"scn":
        return 0, None

    stroke_pattern = _first_name(op2[0])
    fill_pattern = _first_name(op3[0])
    if stroke_pattern is None or stroke_pattern != fill_pattern:
        return 0, None

    fill_index = index + 4
    if fill_index < len(operations) and operations[fill_index][1] == b"gs":
        fill_index += 1
    if fill_index + 1 >= len(operations):
        return 0, None

    rect_op, fill_op = operations[fill_index : fill_index + 2]
    fills_rectangle = rect_op[1] == b"re" and fill_op[1] in {b"f", b"f*"}
    if not fills_rectangle:
        return 0, None

    return fill_index + 2 - index, fill_pattern


def _remove_page_watermark(page, pdf) -> int:
    contents = page.get_contents()
    if contents is None:
        return 0

    stream = ContentStream(contents, pdf)
    old_operations = stream.operations
    new_operations = []
    removed_patterns = set()
    resources = page["/Resources"] if "/Resources" in page else None
    xobjects = resources["/XObject"] if resources and "/XObject" in resources else None
    watermark_images = (
        {name for name, image in xobjects.items() if _is_codecv_image(image)}
        if xobjects else set()
    )
    removed_images = set()
    image_fills = 0
    index = 0

    while index < len(old_operations):
        operands, operator = old_operations[index]
        if operator == b"Do" and len(operands) == 1 and operands[0] in watermark_images:
            removed_images.add(operands[0])
            image_fills += 1
            index += 1
            continue
        matched_count, pattern_name = _is_pattern_fill(old_operations, index)
        if matched_count:
            removed_patterns.add(pattern_name)
            index += matched_count
            continue

        new_operations.append(old_operations[index])
        index += 1

    if not removed_patterns and not removed_images:
        return 0

    stream.operations = new_operations
    replacement = DecodedStreamObject()
    replacement.set_data(stream.get_data())
    page.replace_contents(replacement)

    # Resource dictionaries may be shared by pages or nested forms. Make the
    # edited tables page-local so other streams keep their original resources.
    if resources is not None:
        resources = DictionaryObject(resources)
        page[NameObject("/Resources")] = resources
    patterns = (
        DictionaryObject(resources["/Pattern"])
        if resources and "/Pattern" in resources else None
    )
    if patterns:
        resources[NameObject("/Pattern")] = patterns
        for pattern_name in removed_patterns:
            patterns.pop(NameObject(pattern_name), None)
        if not patterns:
            resources.pop(NameObject("/Pattern"), None)

    if xobjects:
        xobjects = DictionaryObject(xobjects)
        resources[NameObject("/XObject")] = xobjects
        for image_name in removed_images:
            xobjects.pop(image_name, None)
        if not xobjects:
            resources.pop(NameObject("/XObject"), None)

    return len(removed_patterns) + image_fills


def remove_watermark(input_pdf: str | Path, output_pdf: str | Path) -> int:
    """Remove CodeCV pattern or known image watermarks and write a cleaned PDF.

    Returns the number of removed patterns plus image drawing operations.
    """
    input_path = Path(input_pdf)
    output_path = Path(output_pdf)

    reader = PdfReader(str(input_path))
    writer = PdfWriter(clone_from=reader)
    removed = 0

    for page in writer.pages:
        removed += _remove_page_watermark(page, writer)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("wb") as handle:
        writer.write(handle)

    return removed


def default_output_path(input_pdf: Path) -> Path:
    return input_pdf.with_name(input_pdf.stem + DEFAULT_OUTPUT_SUFFIX)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Remove CodeCV's tiled watermark from an exported resume PDF."
    )
    parser.add_argument("input_pdf", type=Path, help="Path to the exported CodeCV PDF")
    parser.add_argument(
        "output_pdf",
        type=Path,
        nargs="?",
        help="Cleaned PDF path. Defaults to '<input>.clean.pdf'.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_pdf = args.output_pdf or default_output_path(args.input_pdf)
    removed = remove_watermark(args.input_pdf, output_pdf)
    print(f"Removed {removed} CodeCV watermark layer(s).")
    print(f"Wrote: {output_pdf}")
    if not removed:
        print("WARNING: No supported watermark found. The output may still contain watermarks.")
    return 0 if removed else 1


if __name__ == "__main__":
    raise SystemExit(main())
