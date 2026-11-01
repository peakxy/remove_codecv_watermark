import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ContentStream, DecodedStreamObject, DictionaryObject, NameObject, NumberObject,
)

import remove_codecv_watermark as remover


def stream(data):
    obj = DecodedStreamObject()
    obj.set_data(data)
    return obj


def image(pixels, alpha):
    obj = stream(pixels)
    mask = stream(alpha)
    for item, color in [(obj, "/DeviceRGB"), (mask, "/DeviceGray")]:
        item.update({
            NameObject("/Subtype"): NameObject("/Image"),
            NameObject("/Width"): NumberObject(2),
            NameObject("/Height"): NumberObject(2),
            NameObject("/BitsPerComponent"): NumberObject(8),
            NameObject("/ColorSpace"): NameObject(color),
        })
    obj[NameObject("/SMask")] = mask
    return obj


class RemovalTests(unittest.TestCase):
    def test_image_roundtrip_preserves_other_artwork_and_text(self):
        pixels, alpha = b"\x80" * 12, b"\x20" * 4
        writer = PdfWriter()
        page = writer.add_blank_page(300, 400)
        objects = DictionaryObject({
            NameObject("/Watermark"): writer._add_object(image(pixels, alpha)),
            NameObject("/Portrait"): writer._add_object(image(pixels, b"\xff" * 4)),
        })
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/XObject"): writer._add_object(objects),
        })
        page.replace_contents(stream(
            b"q /Watermark Do Q q /Portrait Do Q "
            b"BT (Resume body) Tj ET q /Watermark Do Q"
        ))
        second_page = writer.add_blank_page(300, 400)
        second_page[NameObject("/Resources")] = page["/Resources"]
        second_page.replace_contents(stream(b"q /Watermark Do Q"))
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "in.pdf", Path(directory) / "out.pdf"
            writer.write(source)
            with patch.multiple(remover,
                CODECV_IMAGE_SIZE=(2, 2),
                CODECV_IMAGE_SHA256=hashlib.sha256(pixels).hexdigest(),
                CODECV_MASK_SHA256=hashlib.sha256(alpha).hexdigest(),
            ):
                self.assertEqual(
                    remover.remove_watermark(source, output),
                    {"patterns": 0, "images": 2},
                )
                self.assertEqual(
                    remover.remove_watermark(output, output),
                    {"patterns": 0, "images": 0},
                )
            result = PdfReader(output)
            self.assertEqual(len(result.pages), 2)
            ops = ContentStream(result.pages[0].get_contents(), result).operations
            self.assertEqual([a for a, op in ops if op == b"Do"], [["/Portrait"]])
            self.assertIn((["Resume body"], b"Tj"), ops)
            self.assertEqual(list(result.pages[0]["/Resources"]["/XObject"]), ["/Portrait"])
            self.assertEqual(result.pages[0].mediabox, page.mediabox)
            second_ops = ContentStream(result.pages[1].get_contents(), result).operations
            self.assertFalse(any(op == b"Do" for _, op in second_ops))

    def test_unknown_image_keeps_content_and_resources(self):
        writer = PdfWriter()
        page = writer.add_blank_page(300, 400)
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/XObject"): DictionaryObject({
                NameObject("/X11"): image(b"\x80" * 12, b"\x20" * 4),
            }),
        })
        page.replace_contents(stream(b"q /X11 Do Q"))
        original = page.get_contents().get_data()
        self.assertEqual(remover._remove_page_watermark(page, writer), (0, 0))
        self.assertEqual(page.get_contents().get_data(), original)
        self.assertIn("/X11", page["/Resources"]["/XObject"])

    def test_legacy_pattern_and_no_watermark(self):
        writer = PdfWriter()
        page = writer.add_blank_page(300, 400)
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Pattern"): writer._add_object(DictionaryObject({
                NameObject("/P1"): DictionaryObject(),
            })),
        })
        page.replace_contents(stream(
            b"q /Pattern CS /Pattern cs /P1 SCN /P1 scn /G1 gs "
            b"0 0 300 400 re f Q BT (Resume body) Tj ET"
        ))
        second_page = writer.add_blank_page(300, 400)
        second_page[NameObject("/Resources")] = page["/Resources"]
        second_page.replace_contents(stream(page.get_contents().get_data()))
        self.assertEqual(remover._remove_page_watermark(page, writer), (1, 0))
        self.assertEqual(remover._remove_page_watermark(page, writer), (0, 0))
        self.assertNotIn("/Pattern", page["/Resources"])
        self.assertIn(b"Resume body", page.get_contents().get_data())
        self.assertIn("/P1", second_page["/Resources"]["/Pattern"])
        self.assertEqual(remover._remove_page_watermark(second_page, writer), (1, 0))

    def test_geometry_detection_preserves_smaller_draw_of_shared_image(self):
        writer = PdfWriter()
        page = writer.add_blank_page(300, 400)
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/XObject"): DictionaryObject({
                NameObject("/Overlay"): image(b"\x80" * 12, b"\x20" * 4),
            }),
        })
        page.replace_contents(stream(
            b"q 300 0 0 400 0 0 cm /Overlay Do Q "
            b"q 20 0 0 20 0 0 cm /Overlay Do Q"
        ))
        self.assertEqual(remover._remove_page_watermark(page, writer), (0, 1))
        self.assertIn("/Overlay", page["/Resources"]["/XObject"])
        self.assertEqual(
            page.get_contents().operations,
            ContentStream(stream(b"q 20 0 0 20 0 0 cm /Overlay Do Q"), writer).operations,
        )
        self.assertEqual(remover._remove_page_watermark(page, writer), (0, 0))

    def test_geometry_detection_with_shared_page_resources(self):
        writer = PdfWriter()
        resources = DictionaryObject({
            NameObject("/XObject"): writer._add_object(DictionaryObject({
                NameObject("/Overlay"): writer._add_object(image(b"\x80" * 12, b"\x20" * 4)),
            })),
        })
        for _ in range(2):
            page = writer.add_blank_page(300, 400)
            page[NameObject("/Resources")] = resources
            page.replace_contents(stream(b"q 300 0 0 400 0 0 cm /Overlay Do Q"))
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "in.pdf", Path(directory) / "out.pdf"
            writer.write(source)
            self.assertEqual(
                remover.remove_watermark(source, output),
                {"patterns": 0, "images": 2},
            )
            result = PdfReader(output)
            for page in result.pages:
                self.assertNotIn("/XObject", page["/Resources"])
                self.assertFalse(any(op == b"Do" for _, op in page.get_contents().operations))


class CliTests(unittest.TestCase):
    def run_cli(self, directory, *args):
        return subprocess.run(
            [sys.executable, str(Path(remover.__file__).resolve()), *args],
            cwd=directory, capture_output=True, text=True,
        )

    def write_pdf(self, path, with_watermark=False):
        writer = PdfWriter()
        page = writer.add_blank_page(300, 400)
        if with_watermark:
            page[NameObject("/Resources")] = DictionaryObject({
                NameObject("/XObject"): DictionaryObject({
                    NameObject("/Overlay"): image(b"\x80" * 12, b"\x20" * 4),
                }),
            })
            page.replace_contents(stream(b"q 300 0 0 400 0 0 cm /Overlay Do Q"))
        writer.write(path)

    def test_directory_scan_ignores_outputs_and_can_rerun(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "resume.pdf"
            self.write_pdf(source, with_watermark=True)
            for _ in range(2):
                result = self.run_cli(directory)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("1 watermark image(s)", result.stdout)
                self.assertNotIn("WARNING", result.stdout)
                self.assertTrue(source.with_name("resume.clean.pdf").is_file())

    def test_empty_directory_or_multiple_inputs_exit_two(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(self.run_cli(directory).returncode, 2)
            for name in ("first.pdf", "second.pdf"):
                self.write_pdf(Path(directory) / name)
            result = self.run_cli(directory)
            self.assertEqual(result.returncode, 2)
            self.assertIn("first.pdf", result.stdout)
            self.assertIn("second.pdf", result.stdout)
            self.assertFalse(list(Path(directory).glob("*.clean.pdf")))

    def test_explicit_output_and_no_match_warning(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "resume.pdf", Path(directory) / "custom.pdf"
            self.write_pdf(source)
            result = self.run_cli(directory, str(source), str(output))
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("WARNING", result.stdout)
            self.assertTrue(output.is_file())


if __name__ == "__main__":
    unittest.main()
