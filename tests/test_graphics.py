from __future__ import annotations

import base64
import errno
import json
import os
import struct
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path
from unittest.mock import patch

from book_media_platform.graphics import (
    ArtifactIntegrityError,
    GraphicsError,
    fonts,
    load_job,
    render_batch,
)


def write_png(path: Path, width: int, height: int) -> None:
    """Write a small, valid RGBA PNG for renderer-independent contract tests."""
    raw = b"\x00" + (b"\xff\xff\xff\xff" * width)
    image = zlib.compress(raw * height)

    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))

    payload = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", image)
        + chunk(b"IEND", b"")
    )
    path.write_bytes(payload)


class FakeRenderer:
    name = "Fake Renderer"
    version = "test-renderer-1"

    def render(self, svg_path: Path, png_path: Path, width: int, height: int) -> None:
        self.last_svg = svg_path.read_text(encoding="utf-8")
        write_png(png_path, width, height)


class GraphicsContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.assets = self.root / "assets"
        self.assets.mkdir()
        self.spec_path = self.root / "job.json"
        self.output = self.root / "output"
        # Contract tests must not depend on host fonts: provide a local stand-in
        # for the supported Arial file through the documented override.
        fonts = self.root / "fonts"
        fonts.mkdir()
        (fonts / "Arial.ttf").write_bytes(b"contract-test-font")
        self.font_env = patch.dict(os.environ, {"BOOK_MEDIA_FONT_DIRS": str(fonts)})
        self.font_env.start()

    def tearDown(self) -> None:
        self.font_env.stop()
        self.temp.cleanup()

    def write_spec(self, *, template: str = "quote-card", **overrides: object) -> Path:
        spec: dict[str, object] = {
            "schema_version": "graphics.job.v1",
            "job_id": "thai-sample",
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
            "records": [
                {
                    "id": "first-card",
                    "quote": "ระบบที่ดีใช้ logic กับงานที่รู้คำตอบอยู่แล้ว",
                    "author": "ทีมทดลอง",
                }
            ],
        }
        spec.update(overrides)
        if spec.get("records") is None and "records_source" in spec:
            spec.pop("records", None)
        self.spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
        return self.spec_path

    def test_json_job_emits_editable_svg_png_and_zero_model_cost_events(self) -> None:
        spec_path = self.write_spec()

        result = render_batch(spec_path, self.output, renderer=FakeRenderer())

        self.assertEqual(result.status, "accepted")
        self.assertEqual(result.output_count, 3)
        self.assertEqual(result.model_calls, 0)
        self.assertIsNone(result.monetary_cost)
        manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["usage"]["model_calls"], 0)
        self.assertIsNone(manifest["usage"]["monetary_cost"])
        self.assertEqual(manifest["recipe"]["version"], "graphics-template-batch.v1.2")
        self.assertEqual(len(manifest["artifacts"]), 6)
        svg = next(result.manifest_path.parent.rglob("design.svg")).read_text(encoding="utf-8")
        self.assertIn("ระบบที่ดีใช้ logic กับงานที่รู้คำตอบอยู่แล้ว", svg)
        self.assertIn("<text", svg)
        self.assertNotIn("<foreignObject", svg)
        self.assertNotIn('href="http', svg)
        root = ET.fromstring(svg)
        visible_text = next(element for element in root.iter() if element.tag.endswith("}text"))
        self.assertEqual("".join(visible_text.itertext()), "ระบบที่ดีใช้ logic กับงานที่รู้คำตอบอยู่แล้ว")
        for png_path in result.manifest_path.parent.rglob("preview.png"):
            self.assertEqual(png_path.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")

    def test_stale_lock_from_a_crashed_run_does_not_block_the_next_run(self) -> None:
        import socket
        import subprocess
        import sys
        import time

        spec_path = self.write_spec()
        crashed = subprocess.Popen([sys.executable, "-c", "pass"])
        crashed.wait()
        locks = self.output / ".locks"
        locks.mkdir(parents=True)
        (locks / "thai-sample.lock").write_text(
            json.dumps({"pid": crashed.pid, "host": socket.gethostname(), "created_at": time.time()}),
            encoding="utf-8",
        )

        started = time.monotonic()
        result = render_batch(spec_path, self.output, renderer=FakeRenderer())

        self.assertEqual(result.status, "accepted")
        self.assertLess(time.monotonic() - started, 5)
        self.assertFalse((locks / "thai-sample.lock").exists())

    def test_csv_rows_can_feed_the_same_template_contract(self) -> None:
        rows = self.root / "rows.csv"
        rows.write_text(
            "id,quote,author\nfirst-card,ข้อความจาก CSV,ผู้เขียน\n",
            encoding="utf-8-sig",
        )
        self.write_spec(records=None, records_source={"format": "csv", "path": "rows.csv"})

        job = load_job(self.spec_path)
        result = render_batch(job, self.output, renderer=FakeRenderer())

        self.assertEqual(len(job.records), 1)
        self.assertEqual(job.records[0]["quote"], "ข้อความจาก CSV")
        self.assertEqual(result.output_count, 3)

    def test_render_revalidates_mutated_record_ids_before_using_them_as_paths(self) -> None:
        job = load_job(self.write_spec(variants=["square"]))
        job.records[0]["id"] = "../escaped"

        with self.assertRaises(GraphicsError):
            render_batch(job, self.output, renderer=FakeRenderer())

        self.assertFalse((self.output / "escaped").exists())
        self.assertFalse((self.output / job.job_id).exists())

    def test_render_revalidates_mutated_brand_colors_before_svg_compilation(self) -> None:
        job = load_job(self.write_spec(variants=["square"]))
        job.brand.colors["primary"] = '#123456" onload="alert(1)'
        renderer = FakeRenderer()

        with self.assertRaises(GraphicsError):
            render_batch(job, self.output, renderer=renderer)

        self.assertFalse(hasattr(renderer, "last_svg"))
        self.assertFalse((self.output / job.job_id).exists())

    def test_asset_is_embedded_as_data_and_never_kept_as_external_reference(self) -> None:
        # A 1x1 transparent PNG is enough to exercise the local-asset contract.
        image_path = self.assets / "item.png"
        write_png(image_path, 1, 1)
        self.write_spec(
            template="product-card",
            assets_root="assets",
            assets={"product-photo": "item.png"},
            records=[
                {
                    "id": "shoe",
                    "title": "รองเท้าทดลอง",
                    "description": "ผลิตในประเทศ",
                    "price": "฿1,290",
                    "image_asset": "product-photo",
                }
            ],
        )

        result = render_batch(self.spec_path, self.output, renderer=FakeRenderer())
        svg = next(result.manifest_path.parent.rglob("design.svg")).read_text(encoding="utf-8")

        self.assertIn('href="data:image/png;base64,', svg)
        self.assertNotIn("file://", svg)
        self.assertNotIn("https://", svg)
        self.assertIn(base64.b64encode(image_path.read_bytes()).decode("ascii"), svg)

    def test_missing_asset_and_path_escape_are_rejected_before_writing_outputs(self) -> None:
        self.write_spec(
            template="product-card",
            assets_root="assets",
            assets={"photo": "missing.png"},
            records=[{"id": "p", "title": "สินค้า", "image_asset": "photo"}],
        )
        with self.assertRaises(GraphicsError):
            load_job(self.spec_path)
        self.assertFalse(self.output.exists())

        outside = self.root / "outside.png"
        write_png(outside, 1, 1)
        self.write_spec(
            template="product-card",
            assets_root="assets",
            assets={"photo": "../outside.png"},
            records=[{"id": "p", "title": "สินค้า", "image_asset": "photo"}],
        )
        with self.assertRaises(GraphicsError):
            load_job(self.spec_path)

    def test_malformed_sources_and_schema_types_return_product_errors(self) -> None:
        self.write_spec(template=["quote-card"])
        with self.assertRaises(GraphicsError):
            load_job(self.spec_path)

        self.write_spec(records=None, records_source={"format": [], "path": "rows.csv"})
        with self.assertRaises(GraphicsError):
            load_job(self.spec_path)

        self.write_spec(records=None, records_source={"format": "csv", "path": "missing.csv"})
        with self.assertRaises(GraphicsError):
            load_job(self.spec_path)

    def test_svg_assets_and_unknown_fonts_are_rejected(self) -> None:
        (self.assets / "unsafe.svg").write_text(
            '<svg xmlns="http://www.w3.org/2000/svg"><image href="https://example.invalid/x.png"/></svg>',
            encoding="utf-8",
        )
        self.write_spec(
            template="product-card",
            assets_root="assets",
            assets={"photo": "unsafe.svg"},
            records=[{"id": "p", "title": "สินค้า", "image_asset": "photo"}],
        )
        with self.assertRaises(GraphicsError):
            load_job(self.spec_path)

        self.write_spec(
            brand={"colors": {"primary": "#17324D", "accent": "#F2A65A", "background": "#FFFFFF", "foreground": "#000000"}, "font_family": "Missing Family"}
        )
        with self.assertRaises(GraphicsError):
            load_job(self.spec_path)

    def test_long_thai_text_is_preserved_and_unknown_dimensions_fail_closed(self) -> None:
        long_quote = "ภาษาไทยสำหรับทดสอบการจัดบรรทัด " * 5
        self.write_spec(records=[{"id": "long", "quote": long_quote, "author": "ผู้เขียน"}])
        result = render_batch(self.spec_path, self.output, renderer=FakeRenderer())
        svg = next(result.manifest_path.parent.rglob("design.svg")).read_text(encoding="utf-8")
        self.assertIn(long_quote, svg)
        self.assertIn('data-overflow-policy="auto-fit-or-reject"', svg)
        root = ET.fromstring(svg)
        quote_element = next(element for element in root.iter() if element.attrib.get("id") == "quote-text")
        self.assertEqual("".join(quote_element.itertext()), long_quote)

        self.write_spec(records=[{"id": "too-long", "quote": "ภาษาไทย" * 800}])
        with self.assertRaises(GraphicsError):
            render_batch(self.spec_path, self.root / "overflow", renderer=FakeRenderer())

        self.write_spec(variants=["custom-0x9000"])
        with self.assertRaises(GraphicsError):
            load_job(self.spec_path)

    def test_all_three_templates_compile_from_typed_graphic_ir(self) -> None:
        fixtures = {
            "quote-card": {"quote": "คำพูดทดสอบ", "author": "ผู้เขียน"},
            "product-card": {"title": "สินค้า", "image_asset": "photo"},
            "announcement": {"headline": "ประกาศ", "body": "รายละเอียด", "cta": "อ่านเพิ่ม"},
        }
        write_png(self.assets / "photo.png", 1, 1)
        for index, (template, fields) in enumerate(fixtures.items()):
            self.write_spec(
                template=template,
                job_id=f"template-{index}",
                assets_root="assets",
                assets={"photo": "photo.png"} if template == "product-card" else {},
                records=[{"id": "record", **fields}],
                variants=["square"],
            )
            result = render_batch(self.spec_path, self.output, renderer=FakeRenderer())
            self.assertEqual(result.output_count, 1)

    def test_replay_reuses_verified_artifacts_and_detects_tampering(self) -> None:
        spec_path = self.write_spec(variants=["square"])
        first = render_batch(spec_path, self.output, renderer=FakeRenderer())
        replay = render_batch(spec_path, self.output, renderer=FakeRenderer())
        self.assertEqual(first.status, "accepted")
        self.assertEqual(replay.status, "replay")
        self.assertEqual(replay.output_count, first.output_count)
        self.assertGreater(replay.replay_wall_seconds, 0)

        preview = next(first.manifest_path.parent.rglob("preview.png"))
        preview.write_bytes(preview.read_bytes() + b"tampered")
        with self.assertRaises(ArtifactIntegrityError):
            render_batch(spec_path, self.output, renderer=FakeRenderer())

        first.manifest_path.write_text("[]", encoding="utf-8")
        with self.assertRaises(ArtifactIntegrityError):
            render_batch(spec_path, self.output, renderer=FakeRenderer())

    def test_replay_rejects_manifest_job_identity_mismatch(self) -> None:
        spec_path = self.write_spec(variants=["square"])
        first = render_batch(spec_path, self.output, renderer=FakeRenderer())
        manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
        manifest["job_id"] = "different-job"
        first.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        with self.assertRaises(ArtifactIntegrityError):
            render_batch(spec_path, self.output, renderer=FakeRenderer())

    def test_replay_rejects_manifest_recipe_and_renderer_mismatch(self) -> None:
        for name, section, key, value in (
            ("recipe", "recipe", "version", "graphics-template-batch.invalid"),
            ("renderer", "renderer", "version", "different-renderer"),
        ):
            with self.subTest(name=name):
                spec_path = self.write_spec(job_id=f"manifest-{name}", variants=["square"])
                output = self.root / f"output-{name}"
                first = render_batch(spec_path, output, renderer=FakeRenderer())
                manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
                manifest[section][key] = value
                first.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

                with self.assertRaises(ArtifactIntegrityError):
                    render_batch(spec_path, output, renderer=FakeRenderer())

    def test_replay_rejects_artifact_pairs_that_do_not_match_the_job(self) -> None:
        spec_path = self.write_spec(variants=["square"])
        first = render_batch(spec_path, self.output, renderer=FakeRenderer())
        manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
        for artifact in manifest["artifacts"]:
            artifact["record_id"] = "other-card"
            artifact["path"] = artifact["path"].replace("first-card/", "other-card/")
        original_dir = first.manifest_path.parent / "first-card"
        moved_dir = first.manifest_path.parent / "other-card"
        original_dir.rename(moved_dir)
        first.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        with self.assertRaises(ArtifactIntegrityError):
            render_batch(spec_path, self.output, renderer=FakeRenderer())

    def test_atomic_commit_supports_deep_output_roots(self) -> None:
        self.write_spec(job_id="long-output", variants=["square"])
        padding = max(1, 182 - len(str(self.root.resolve())) - 1)
        deep_output = self.root / ("o" * padding)

        result = render_batch(self.spec_path, deep_output, renderer=FakeRenderer())

        self.assertEqual(result.status, "accepted")
        self.assertTrue(result.manifest_path.is_file())

    def test_atomic_commit_retries_transient_windows_access_denial(self) -> None:
        self.write_spec(job_id="transient-commit", variants=["square"])
        real_replace = os.replace
        attempts = 0

        def replace_with_one_transient_lock(source: object, destination: object) -> None:
            nonlocal attempts
            if Path(source).is_dir() and attempts == 0:
                attempts += 1
                raise PermissionError(errno.EACCES, "Access is denied")
            real_replace(source, destination)

        with patch("book_media_platform.graphics.service.os.replace", side_effect=replace_with_one_transient_lock):
            result = render_batch(self.spec_path, self.output, renderer=FakeRenderer())

        self.assertEqual(attempts, 1)
        self.assertEqual(result.status, "accepted")


if __name__ == "__main__":
    unittest.main()


class FontDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_configured_font_directory_is_searched_first_and_case_insensitively(self) -> None:
        first = self.root / "first"
        second = self.root / "second"
        first.mkdir()
        second.mkdir()
        (second / "DEJAVUSANS.TTF").write_bytes(b"font")
        value = os.pathsep.join([str(first), str(second)])
        with patch.dict(os.environ, {fonts.FONT_DIRS_ENV: value}):
            self.assertEqual(fonts.find_font_file("DejaVu Sans"), second / "DEJAVUSANS.TTF")
            self.assertEqual(fonts.require_font("DejaVu Sans"), second / "DEJAVUSANS.TTF")

    def test_relative_configured_font_directories_are_ignored(self) -> None:
        with patch.dict(os.environ, {fonts.FONT_DIRS_ENV: os.pathsep.join(["relative-fonts", ""])}):
            self.assertEqual(fonts._configured_font_roots(), [])

    def test_unknown_family_and_missing_file_are_distinct_errors(self) -> None:
        with patch.dict(os.environ, {fonts.FONT_DIRS_ENV: str(self.root)}), patch.object(
            fonts, "_font_roots", return_value=(self.root,)
        ):
            with self.assertRaisesRegex(GraphicsError, "unsupported font family"):
                fonts.require_font("Comic Sans")
            with self.assertRaisesRegex(GraphicsError, "not installed: Tahoma"):
                fonts.require_font("Tahoma")
