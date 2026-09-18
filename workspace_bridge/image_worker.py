"""Fixed, isolated image decoder. Receives authorized bytes on stdin, never paths.

Executed by the bridge with `python -I -B <this installed file> <dimension>`.
It is resource-bounded, NOT an OS/network sandbox against decoder vulnerabilities.
No user command, path, URL, plugin, or converter is accepted.
"""
from __future__ import annotations

import base64
from io import BytesIO
import json
import resource
import sys
import warnings

MAX_INPUT_BYTES = 20 * 1024 * 1024
MAX_PIXELS = 40_000_000
MAX_PREVIEW_BYTES = 2 * 1024 * 1024
MAX_DIMENSION = 4096
DEFAULT_DIMENSION = 2048
SUPPORTED_FORMATS = ("PNG", "JPEG", "WEBP", "GIF", "BMP", "TIFF")


class PreviewLimit(Exception):
    pass


class BoundedBuffer(BytesIO):
    def write(self, data):
        if self.tell() + len(data) > MAX_PREVIEW_BYTES:
            raise PreviewLimit()
        return super().write(data)


def constrain_process():
    # Applied before importing native image codecs. Hard wall time is in the parent.
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CPU, (8, 8))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    # macOS does not reliably support RLIMIT_AS; no memory-limit claim there.
    if sys.platform.startswith("linux"):
        resource.setrlimit(resource.RLIMIT_AS, (1024 * 1024 * 1024,) * 2)


def convert(data: bytes, dimension: int) -> dict:
    from PIL import Image, ImageFile, ImageOps, PngImagePlugin

    if not data or len(data) > MAX_INPUT_BYTES:
        return {"error": "image_limit"}
    if type(dimension) is not int or not 256 <= dimension <= MAX_DIMENSION:
        return {"error": "invalid_arguments"}
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    ImageFile.LOAD_TRUNCATED_IMAGES = False
    PngImagePlugin.MAX_TEXT_CHUNK = 1024 * 1024
    PngImagePlugin.MAX_TEXT_MEMORY = 2 * 1024 * 1024
    warnings.simplefilter("error", Image.DecompressionBombWarning)

    # Only these raster decoders are attempted; notably no EPS/SVG/PDF rendering.
    with Image.open(BytesIO(data), formats=SUPPORTED_FORMATS) as check:
        check.verify()
    with Image.open(BytesIO(data), formats=SUPPORTED_FORMATS) as original:
        original.seek(0)
        source_format = original.format
        source_width, source_height = original.size
        if source_width * source_height > MAX_PIXELS:
            return {"error": "image_limit"}
        original.load()
        orientation = original.getexif().get(274, 1)
        oriented = ImageOps.exif_transpose(original)
        with oriented:
            oriented_size = oriented.size
            alpha = "A" in oriented.getbands() or "transparency" in oriented.info
            preview = oriented.convert("RGBA" if alpha else "RGB")
            try:
                preview.thumbnail((dimension, dimension), Image.Resampling.LANCZOS)
                # Build a fresh image so EXIF/GPS, ICC, XMP, comments and trailing
                # payloads cannot ride through Pillow's implicit metadata copying.
                clean = Image.new(preview.mode, preview.size)
                clean.paste(preview)
            finally:
                preview.close()
    mime_type = "image/jpeg" if source_format == "JPEG" else "image/png"
    output_format = "JPEG" if mime_type == "image/jpeg" else "PNG"
    reduced_for_bytes = False
    try:
        # Worst-case noisy PNGs may require smaller dimensions to fit the byte cap.
        for _ in range(12):
            try:
                with BoundedBuffer() as output:
                    if output_format == "JPEG":
                        clean.save(output, "JPEG", quality=88, subsampling=0)
                    else:
                        clean.save(output, "PNG", compress_level=6)
                    encoded = output.getvalue()
                break
            except PreviewLimit:
                if max(clean.size) <= 256:
                    return {"error": "image_limit"}
                new_size = (max(1, int(clean.width * .75)), max(1, int(clean.height * .75)))
                smaller = clean.resize(new_size, Image.Resampling.LANCZOS)
                clean.close()
                clean = smaller
                reduced_for_bytes = True
        else:
            return {"error": "image_limit"}
        return {
            "data": base64.b64encode(encoded).decode("ascii"),
            "mime_type": mime_type,
            "source_format": source_format,
            "source_width": source_width, "source_height": source_height,
            "oriented_width": oriented_size[0], "oriented_height": oriented_size[1],
            "width": clean.width, "height": clean.height,
            "orientation_applied": orientation in (2, 3, 4, 5, 6, 7, 8),
            "resized": clean.size != oriented_size,
            "reduced_for_byte_limit": reduced_for_bytes,
            "lossy_encoding": output_format == "JPEG",
            "alpha_preserved": alpha,
        }
    finally:
        clean.close()


def main() -> int:
    try:
        constrain_process()
        if len(sys.argv) != 2:
            raise ValueError()
        dimension = int(sys.argv[1])
        data = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        result = convert(data, dimension)
    except ImportError:
        result = {"error": "image_decoder_unavailable"}
    except MemoryError:
        result = {"error": "image_limit"}
    except Exception as exc:
        # No decoder messages, file contents, embedded metadata or tracebacks leak.
        code = "image_limit" if type(exc).__name__ in (
            "DecompressionBombWarning", "DecompressionBombError") else "invalid_image"
        result = {"error": code}
    sys.stdout.write(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
