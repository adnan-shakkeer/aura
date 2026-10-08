"""Selective image validation and processing for Admin Product Management.

Scope (AURA Image Standard v1):

- This module NEVER processes untouched existing images. Callers must only
  pass explicitly edited images (or new uploads requiring validation).
- No background removal / replacement / generation of any kind.
- No automatic crop-box selection and no generic "cover" cropping. The only
  geometric operation is cropping the admin-selected square region and
  resizing it proportionally to the target size.
- Because the editor constrains the crop frame to stay inside the source
  image, every output pixel originates from the source image. There is
  intentionally NO fill/background code path in this module.
"""

import uuid

from io import BytesIO

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

from PIL import Image, ImageOps


ALLOWED_IMAGE_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
}

MIN_VARIANT_IMAGES = 3
MAX_VARIANT_IMAGES = 5

# Hard safety bounds (abuse / decompression-bomb protection). These are NOT
# content requirements and do not conflict with the Image Editor: any image
# the editor can load in a browser falls far below these caps.
MAX_UPLOAD_BYTES = 15 * 1024 * 1024
MAX_IMAGE_PIXELS = 50_000_000

# Final 1:1 composition size for explicitly edited images.
TARGET_SIZE = 1200

ERROR_INVALID_TYPE = (
    "Only JPEG, PNG, and WebP images are allowed."
)
ERROR_INVALID_IMAGE = (
    "One or more uploaded files are not valid images."
)
ERROR_TOO_LARGE = (
    "Image file is too large. Please use an image under 15 MB."
)
ERROR_TOO_MANY_PIXELS = (
    "Image dimensions are too large to process safely."
)

_FORMAT_BY_MIME = {
    "image/jpeg": "JPEG",
    "image/png": "PNG",
    "image/webp": "WEBP",
}

_EXTENSION_BY_FORMAT = {
    "JPEG": "jpg",
    "PNG": "png",
    "WEBP": "webp",
}


def validate_image_file(uploaded_file):
    """Validate a single incoming image file. Raises ValidationError."""
    content_type = getattr(uploaded_file, "content_type", None)

    if content_type not in ALLOWED_IMAGE_TYPES:
        raise ValidationError(ERROR_INVALID_TYPE)

    size = getattr(uploaded_file, "size", None)

    if size is not None and size > MAX_UPLOAD_BYTES:
        raise ValidationError(ERROR_TOO_LARGE)

    try:
        image = Image.open(uploaded_file)
        image.verify()
        uploaded_file.seek(0)
        image = Image.open(uploaded_file)
        width, height = image.size
    except Exception:
        raise ValidationError(ERROR_INVALID_IMAGE)

    try:
        uploaded_file.seek(0)
    except Exception:
        pass

    if width * height > MAX_IMAGE_PIXELS:
        raise ValidationError(ERROR_TOO_MANY_PIXELS)

    return True


def _clamp(value, minimum, maximum):
    return max(minimum, min(maximum, value))


def get_image_info(uploaded_file):
    """Return (format, width, height) without trusting caller input."""
    try:
        uploaded_file.seek(0)
        image = Image.open(uploaded_file)
        info = ((image.format or "").upper(), image.size[0], image.size[1])
    except Exception:
        raise ValidationError(ERROR_INVALID_IMAGE)

    try:
        uploaded_file.seek(0)
    except Exception:
        pass

    return info


def _normalize_crop(transform):
    """Normalize an admin-selected square crop dict.

    Expected keys (all optional, all clamped server-side because the
    frontend must never be trusted):

    - left / top: top-left corner of the selected square, as fractions
      of the source width / height.
    - side: side length of the selected square, as a fraction of the
      source width (the selection is square in source pixels because the
      editor only applies uniform scaling).
    """
    transform = transform or {}

    def _as_float(value, default):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    return {
        "left": _clamp(_as_float(transform.get("left", 0.0), 0.0), 0.0, 1.0),
        "top": _clamp(_as_float(transform.get("top", 0.0), 0.0), 0.0, 1.0),
        "side": _clamp(_as_float(transform.get("side", 1.0), 1.0), 0.0, 1.0),
    }


def render_standard_image(source_file, transform=None):
    """Render the admin-approved square composition for an EDITED image.

    - Opens the source safely and applies EXIF orientation.
    - Crops the admin-selected square region (``left`` / ``top`` /
      ``side`` fractions of the source dimensions). The region is forced
      inside the source bounds; no padding or background fill is ever
      required or performed.
    - Resizes the selected square PROPORTIONALLY to the TARGET_SIZE
      square (single uniform factor: no stretch possible).
    - Preserves the source format tier (JPEG stays JPEG, PNG/WebP keep
      transparency). No background compositing is performed.

    Returns a Django ContentFile (caller assigns the storage name).
    """
    crop = _normalize_crop(transform)

    source_file.seek(0)
    image = Image.open(source_file)
    source_format = (image.format or "JPEG").upper()
    image = ImageOps.exif_transpose(image)

    if source_format == "MPO":
        source_format = "JPEG"

    if source_format not in _EXTENSION_BY_FORMAT:
        source_format = "JPEG"

    width, height = image.size

    if width <= 0 or height <= 0:
        raise ValidationError(ERROR_INVALID_IMAGE)

    side = max(1, int(round(crop["side"] * width)))
    left = int(round(crop["left"] * width))
    top = int(round(crop["top"] * height))

    # Force the selection inside the source image. The editor already
    # guarantees this; clamping here only guards tampered parameters.
    side = min(side, width, height)
    left = min(max(left, 0), width - side)
    top = min(max(top, 0), height - side)

    image = image.crop((left, top, left + side, top + side))

    image = image.resize(
        (TARGET_SIZE, TARGET_SIZE),
        Image.LANCZOS,
    )

    buffer = BytesIO()

    if source_format == "JPEG":
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        image.save(
            buffer,
            format="JPEG",
            quality=90,
            optimize=True,
        )
    elif source_format == "PNG":
        image.save(buffer, format="PNG", optimize=True)
    else:
        image.save(buffer, format="WEBP", quality=90, method=6)

    return ContentFile(buffer.getvalue())


def build_standard_filename(original_name, source_format="JPEG"):
    """Build a collision-free storage name for a rendered standard image."""
    extension = _EXTENSION_BY_FORMAT.get(
        (source_format or "JPEG").upper(),
        "jpg",
    )
    return f"products/variants/{uuid.uuid4().hex}.{extension}"


def save_replacement_image(variant_image, uploaded_file):
    """Persist a new file for an existing VariantImage row.

    The old physical file is NOT deleted here. Returns the old storage name
    so the caller can schedule its deletion via transaction.on_commit()
    after a successful commit.
    """
    old_name = (
        variant_image.image.name
        if getattr(variant_image, "image", None)
        else ""
    )

    original_name = getattr(uploaded_file, "name", "") or "image"
    extension = original_name.rsplit(".", 1)[-1].lower() if "." in original_name else "jpg"

    if extension not in ("jpg", "jpeg", "png", "webp"):
        extension = "jpg"

    new_name = f"products/variants/{uuid.uuid4().hex}.{extension}"
    uploaded_file.seek(0)
    saved_name = default_storage.save(new_name, uploaded_file)

    variant_image.image = saved_name
    variant_image.save(update_fields=["image"])

    return old_name
