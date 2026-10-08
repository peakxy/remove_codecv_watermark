import hashlib
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
                self.assertEqual(remover.remove_watermark(source, output), 3)
                self.assertEqual(remover.remove_watermark(output, output), 0)
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
        self.assertEqual(remover._remove_page_watermark(page, writer), 0)
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
        self.assertEqual(remover._remove_page_watermark(page, writer), 1)
        self.assertEqual(remover._remove_page_watermark(page, writer), 0)
        self.assertNotIn("/Pattern", page["/Resources"])
        self.assertIn(b"Resume body", page.get_contents().get_data())


if __name__ == "__main__":
    unittest.main()
