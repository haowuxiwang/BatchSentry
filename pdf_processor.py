import fitz  # PyMuPDF
import os
import base64
from typing import List


def extract_pages_as_images(pdf_path: str, output_dir: str, dpi: int = 200) -> List[str]:
    """Extract each page from PDF as PNG image. Returns list of image paths."""
    os.makedirs(output_dir, exist_ok=True)
    doc = fitz.open(pdf_path)
    image_paths = []

    for i, page in enumerate(doc):
        # Render page to image
        mat = fitz.Matrix(dpi / 72, dpi / 72)
        pix = page.get_pixmap(matrix=mat)
        image_path = os.path.join(output_dir, f"page_{i + 1:03d}.png")
        pix.save(image_path)
        image_paths.append(image_path)

    doc.close()
    return image_paths


def image_to_base64(image_path: str) -> str:
    """Read image file and return base64 encoded string."""
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def get_page_count(pdf_path: str) -> int:
    """Return total number of pages in PDF."""
    doc = fitz.open(pdf_path)
    count = len(doc)
    doc.close()
    return count
