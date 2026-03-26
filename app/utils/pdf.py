"""PDF utilities — document detection, crop, and multi-page PDF assembly."""

from __future__ import annotations

import logging
from io import BytesIO

import numpy as np

logger = logging.getLogger(__name__)


def _detect_and_crop(img):
    """Detect document in photo and crop to its edges.

    Uses adaptive thresholding to find the white/light paper region
    on any background (dark table, colored surface, etc.)
    """
    import cv2
    from PIL import Image

    cv_img = np.array(img)
    if len(cv_img.shape) == 2:
        gray = cv_img
        cv_img = cv2.cvtColor(cv_img, cv2.COLOR_GRAY2BGR)
    else:
        gray = cv2.cvtColor(cv_img, cv2.COLOR_RGB2GRAY)

    h, w = gray.shape

    # Skip tiny images
    if w < 100 or h < 100:
        return img

    # Check contrast: edges vs center
    margin = max(int(min(w, h) * 0.04), 8)
    edge_strips = np.concatenate([
        gray[:margin, :].flatten(),
        gray[-margin:, :].flatten(),
        gray[:, :margin].flatten(),
        gray[:, -margin:].flatten(),
    ])
    edge_mean = float(np.mean(edge_strips))
    center = gray[h // 4: 3 * h // 4, w // 4: 3 * w // 4]
    center_mean = float(np.mean(center))

    # Need contrast between edge and center to crop
    if center_mean - edge_mean < 25:
        return img  # No clear background/foreground separation

    # Use Otsu's threshold — automatically finds optimal split
    blurred = cv2.GaussianBlur(gray, (7, 7), 0)
    _, binary = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    # Morphological close — fill text holes, connect paper regions
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (25, 25))
    closed = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)

    # Find the largest white connected component (the paper)
    num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(closed, connectivity=8)

    if num_labels < 2:
        return img

    # Skip label 0 (background), find largest component
    largest_label = 1 + np.argmax(stats[1:, cv2.CC_STAT_AREA])
    largest_area = stats[largest_label, cv2.CC_STAT_AREA]

    # Paper should be 30-99% of image (even small borders are worth cropping)
    area_ratio = largest_area / (w * h)
    if area_ratio < 0.30 or area_ratio > 0.99:
        return img

    # Get bounding box of the paper
    x = stats[largest_label, cv2.CC_STAT_LEFT]
    y = stats[largest_label, cv2.CC_STAT_TOP]
    rw = stats[largest_label, cv2.CC_STAT_WIDTH]
    rh = stats[largest_label, cv2.CC_STAT_HEIGHT]

    # Add tiny padding
    pad = max(int(min(w, h) * 0.003), 2)
    x1 = max(0, x - pad)
    y1 = max(0, y - pad)
    x2 = min(w, x + rw + pad)
    y2 = min(h, y + rh + pad)

    # Verify we actually removed something (at least 1%)
    cropped_area = (x2 - x1) * (y2 - y1)
    if cropped_area >= w * h * 0.99:
        return img  # Barely any crop

    logger.info(f"Document crop: {w}x{h} → {x2-x1}x{y2-y1} (paper={area_ratio:.0%})")
    result = cv_img[y1:y2, x1:x2]
    return Image.fromarray(result)


def images_to_pdf(image_bytes_list: list[bytes], auto_crop: bool = True) -> bytes:
    """Combine multiple images into a single PDF document.

    Each image is auto-cropped (document detection)
    and assembled into a multi-page PDF.
    """
    from PIL import Image

    images = []
    for i, data in enumerate(image_bytes_list):
        img = Image.open(BytesIO(data))
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        if auto_crop:
            original_size = img.size
            img = _detect_and_crop(img)
            if img.size != original_size:
                logger.info(f"Page {i+1}: cropped {original_size} → {img.size}")
        images.append(img)

    if not images:
        raise ValueError("No images to combine")

    output = BytesIO()
    if len(images) == 1:
        images[0].save(output, format="PDF")
    else:
        images[0].save(output, format="PDF", save_all=True, append_images=images[1:])

    return output.getvalue()
