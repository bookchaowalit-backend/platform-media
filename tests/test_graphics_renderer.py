from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path
from typing import Any

from book_media_platform.graphics.errors import GraphicsError
from book_media_platform.graphics.fonts import find_font_file
from book_media_platform.graphics.qa import inspect_png
from book_media_platform.graphics.renderer import EdgeRenderer, _run_edge, find_edge
from book_media_platform.graphics.service import RECIPE_VERSION, render_batch


def write_pattern_png(path: Path) -> None:
    width = height = 96

    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))

    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for x in range(width):
            if 38 <= x <= 58 and 5 <= y <= 21:
                color = (23, 50, 77, 255)
            elif 28 <= x <= 68 and 19 <= y <= 82:
                color = (242, 166, 90, 255)
            elif 33 <= x <= 63 and 43 <= y <= 61:
                color = (255, 255, 255, 255)
            else:
                color = (232, 238, 245, 255)
            rows.extend(color)

    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(rows)))
        + chunk(b"IEND", b"")
    )


def _require_local_renderer(test: unittest.TestCase, *, font_family: str | None = "Arial") -> None:
    """Skip real-renderer tests when Edge or the reviewed font is unavailable."""

    try:
        find_edge()
    except GraphicsError as exc:
        test.skipTest(str(exc))
    if font_family is not None and find_font_file(font_family) is None:
        test.skipTest(f"{font_family} font is not installed")


class EdgeRendererIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        if os.environ.get("BOOK_MEDIA_SKIP_RENDERER_TESTS") == "1":
            self.skipTest("renderer integration tests disabled by BOOK_MEDIA_SKIP_RENDERER_TESTS=1")

    def test_nine_visual_previews_match_reviewed_golden_images(self) -> None:
        try:
            edge = EdgeRenderer(find_edge())
        except GraphicsError as exc:
            self.skipTest(str(exc))
        font_path = find_font_file("Arial")
        if font_path is None:
            self.skipTest("Arial font is not installed")
        font_hash = hashlib.sha256(font_path.read_bytes()).hexdigest()
        golden_dir = Path(__file__).parent / "golden" / "graphics-v1a"
        manifest_path = golden_dir / "manifest.json"
        templates: dict[str, dict[str, Any]] = {
            "quote-card": {
                "records": [
                    {
                        "id": "quote-golden",
                        "quote": "ระบบที่ดีให้กฎชัดเจนกับงานซ้ำ และใช้ AI เฉพาะตรงที่ต้องตีความ",
                        "author": "ทีมผลิตภัณฑ์",
                    }
                ]
            },
            "product-card": {
                "records": [
                    {
                        "id": "product-golden",
                        "title": "ชาสมุนไพร",
                        "description": "ผลิตจากวัตถุดิบท้องถิ่น คัดคุณภาพ มีข้อมูลครบ และจัดส่งภายในหนึ่งถึงสองวัน",
                        "price": "฿1,290",
                        "image_asset": "photo",
                    }
                ]
            },
            "announcement": {
                "records": [
                    {
                        "id": "announcement-golden",
                        "headline": "อัปเดตบริการใหม่สำหรับผู้ใช้งาน",
                        "body": "เพิ่มรูปแบบการส่งออกหลายขนาด พร้อมเก็บข้อความและองค์ประกอบเพื่อแก้ไขต่อได้",
                        "cta": "ดูรายละเอียด",
                    }
                ]
            },
        }
        actual: dict[str, bytes] = {}
        expected_heights = {"square": 1080, "portrait": 1350, "story": 1920}
        with tempfile.TemporaryDirectory(prefix="graphics visual golden ") as temp_dir:
            root = Path(temp_dir)
            for template, template_data in templates.items():
                job: dict[str, Any] = {
                    "schema_version": "graphics.job.v1",
                    "job_id": f"visual-{template}",
                    "template": template,
                    "brand": {
                        "colors": {
                            "primary": "#17324D",
                            "accent": "#F2A65A",
                            "background": "#F8F5EF",
                            "foreground": "#14212B",
                            "surface": "#FFFFFF",
                        },
                        "font_family": "Arial",
                    },
                    "variants": ["square", "portrait", "story"],
                    **template_data,
                }
                if template == "product-card":
                    assets = root / "assets"
                    assets.mkdir()
                    write_pattern_png(assets / "photo.png")
                    job["assets_root"] = "assets"
                    job["assets"] = {"photo": "photo.png"}
                spec = root / f"{template}.json"
                spec.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
                result = render_batch(spec, root / "output", renderer=edge)
                self.assertEqual(result.output_count, 3)
                records = template_data["records"]
                record_id = records[0]["id"]
                for variant in job["variants"]:
                    preview = result.manifest_path.parent / record_id / variant / "preview.png"
                    self.assertEqual(inspect_png(preview, 1080, expected_heights[variant])[:2], (1080, expected_heights[variant]))
                    actual[f"{template}-{variant}"] = preview.read_bytes()

        baseline = {
            "schema_version": "graphics.visual-golden.v1",
            "recipe_version": RECIPE_VERSION,
            "renderer": {"name": edge.name, "version": edge.version},
            "font": {"family": "Arial", "sha256": font_hash},
            "images": {key: hashlib.sha256(data).hexdigest() for key, data in sorted(actual.items())},
        }
        if os.environ.get("BOOK_MEDIA_GRAPHICS_UPDATE_GOLDENS") == "1":
            golden_dir.mkdir(parents=True, exist_ok=True)
            for key, data in actual.items():
                (golden_dir / f"{key}.png").write_bytes(data)
            manifest_path.write_text(json.dumps(baseline, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return

        self.assertTrue(manifest_path.is_file(), "reviewed visual golden set is missing")
        expected = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(expected["recipe_version"], RECIPE_VERSION)
        if expected["renderer"] != baseline["renderer"] or expected["font"] != baseline["font"]:
            self.skipTest("raster comparison needs the reviewed Edge version and Arial font fingerprint")
        self.assertEqual(expected["images"], baseline["images"])

    @unittest.skipUnless(os.name == "nt", "Windows renderer process-tree behavior")
    def test_timeout_terminates_the_supervisor_and_its_child_process(self) -> None:
        powershell = shutil.which("powershell.exe") or shutil.which("powershell")
        taskkill = shutil.which("taskkill.exe") or shutil.which("taskkill")
        if not powershell or not taskkill:
            self.skipTest("Windows PowerShell and taskkill are required")

        with tempfile.TemporaryDirectory(prefix="graphics timeout cleanup ") as temp_dir:
            root = Path(temp_dir)
            profile = root / "unique-renderer-profile"
            marker = str(profile.resolve())
            child_script = f"$marker='{marker}'; Start-Sleep -Seconds 60"
            args = [powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", child_script]
            with self.assertRaises(subprocess.TimeoutExpired):
                _run_edge(
                    args,
                    2,
                    screenshot_path=root / "preview.png",
                    profile_path=profile,
                )

            query = (
                "$matches=@(Get-CimInstance -ClassName Win32_Process -Filter \"Name='powershell.exe'\" "
                f"| Where-Object {{ $_.ProcessId -ne $PID -and $_.CommandLine -and $_.CommandLine.Contains('{marker}') }}); "
                "($matches | ForEach-Object { $_.ProcessId }) -join ','"
            )
            result = subprocess.run(
                [powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", query],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            remaining = [value for value in result.stdout.strip().split(",") if value]
            try:
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(remaining, [], "renderer child process remained after timeout")
            finally:
                for process_id in remaining:
                    subprocess.run(
                        [taskkill, "/PID", process_id, "/T", "/F"],
                        capture_output=True,
                        timeout=10,
                        check=False,
                    )

    def test_headless_edge_waits_for_a_complete_local_preview(self) -> None:
        try:
            renderer = EdgeRenderer(find_edge())
        except GraphicsError as exc:
            self.skipTest(str(exc))
        with tempfile.TemporaryDirectory(prefix="graphics renderer test ") as temp_dir:
            root = Path(temp_dir)
            svg = root / "design.svg"
            png = root / "preview.png"
            svg.write_text(
                '<svg xmlns="http://www.w3.org/2000/svg" width="1080" height="1080" viewBox="0 0 1080 1080">'
                '<rect width="1080" height="1080" fill="#ffffff"/>'
                '<text x="80" y="540" font-family="Arial" font-size="72">Local preview</text></svg>',
                encoding="utf-8",
            )

            renderer.render(svg, png, 1080, 1080)

            self.assertEqual(inspect_png(png, 1080, 1080)[:2], (1080, 1080))

    def test_real_render_and_atomic_commit_work_under_deep_output_root(self) -> None:
        _require_local_renderer(self)
        with tempfile.TemporaryDirectory(prefix="graphics deep output ") as temp_dir:
            root = Path(temp_dir)
            job = {
                "schema_version": "graphics.job.v1",
                "job_id": "graphics-acceptance-product-card",
                "template": "quote-card",
                "brand": {
                    "colors": {
                        "primary": "#17324D",
                        "accent": "#F2A65A",
                        "background": "#F8F5EF",
                        "foreground": "#14212B",
                        "surface": "#FFFFFF",
                    },
                    "font_family": "Arial",
                },
                "variants": ["story"],
                "records": [{"id": "product-01", "quote": "Long local path acceptance"}],
            }
            spec = root / "job.json"
            spec.write_text(json.dumps(job), encoding="utf-8")
            padding = max(1, 182 - len(str(root.resolve())) - 1)
            output = root / ("o" * padding)

            result = render_batch(spec, output)

            self.assertEqual(result.output_count, 1)
            self.assertTrue(result.manifest_path.is_file())

    def test_real_product_template_embeds_and_renders_local_png_asset(self) -> None:
        _require_local_renderer(self)
        with tempfile.TemporaryDirectory(prefix="graphics product asset ") as temp_dir:
            root = Path(temp_dir)
            assets = root / "assets"
            assets.mkdir()
            image = assets / "photo.png"

            def chunk(kind: bytes, payload: bytes) -> bytes:
                body = kind + payload
                return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))

            image.write_bytes(
                b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(b"\x00\x40\x90\xe0\xff"))
                + chunk(b"IEND", b"")
            )
            job = {
                "schema_version": "graphics.job.v1",
                "job_id": "product-asset-render",
                "template": "product-card",
                "brand": {
                    "colors": {
                        "primary": "#17324D",
                        "accent": "#F2A65A",
                        "background": "#F8F5EF",
                        "foreground": "#14212B",
                        "surface": "#FFFFFF",
                    },
                    "font_family": "Arial",
                },
                "variants": ["square"],
                "assets_root": "assets",
                "assets": {"photo": "photo.png"},
                "records": [
                    {
                        "id": "product-01",
                        "title": "สินค้าทดลอง",
                        "description": "ภาพนี้มาจากไฟล์ในเครื่อง",
                        "price": "฿1,290",
                        "image_asset": "photo",
                    }
                ],
            }
            spec = root / "product-job.json"
            spec.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")

            result = render_batch(spec, root / "output")

            preview = result.manifest_path.parent / "product-01" / "square" / "preview.png"
            self.assertEqual(result.output_count, 1)
            self.assertEqual(inspect_png(preview, 1080, 1080)[:2], (1080, 1080))

    def test_long_product_description_stays_clear_of_price_in_all_variants(self) -> None:
        _require_local_renderer(self)
        with tempfile.TemporaryDirectory(prefix="graphics product text spacing ") as temp_dir:
            root = Path(temp_dir)
            assets = root / "assets"
            assets.mkdir()
            image = assets / "photo.png"

            def chunk(kind: bytes, payload: bytes) -> bytes:
                body = kind + payload
                return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))

            image.write_bytes(
                b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(b"\x00\x40\x90\xe0\xff"))
                + chunk(b"IEND", b"")
            )
            job: dict[str, Any] = {
                "schema_version": "graphics.job.v1",
                "job_id": "product-text-spacing",
                "template": "product-card",
                "brand": {
                    "colors": {
                        "primary": "#17324D",
                        "accent": "#F2A65A",
                        "background": "#F8F5EF",
                        "foreground": "#14212B",
                        "surface": "#FFFFFF",
                    },
                    "font_family": "Arial",
                },
                "variants": ["square", "portrait", "story"],
                "assets_root": "assets",
                "assets": {"photo": "photo.png"},
                "records": [
                    {
                        "id": "long-copy",
                        "title": "สินค้าทดลอง",
                        "description": "รายละเอียดสินค้าทดลองสำหรับผู้ที่มองหาคุณภาพและการจัดส่งที่รวดเร็วจากผู้ผลิตในประเทศ",
                        "price": "฿1,290",
                        "image_asset": "photo",
                    }
                ],
            }
            spec = root / "job.json"
            spec.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")

            result = render_batch(spec, root / "output")

            self.assertEqual(result.output_count, 3)
            for variant in job["variants"]:
                svg = result.manifest_path.parent / "long-copy" / variant / "design.svg"
                elements = ET.parse(svg).getroot()
                text_boxes = {
                    item.attrib["id"]: item
                    for item in elements.iter()
                    if item.attrib.get("id") in {"product-description", "product-price"}
                }
                description = text_boxes["product-description"]
                price = text_boxes["product-price"]
                description_right = float(description.attrib["data-box-x"]) + float(
                    description.attrib["data-box-width"]
                )
                price_left = float(price.attrib["data-box-x"])
                self.assertLessEqual(description_right, price_left)


if __name__ == "__main__":
    unittest.main()
