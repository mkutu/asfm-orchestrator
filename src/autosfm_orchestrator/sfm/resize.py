"""Photo downscaling for AutoSfM.

Ported from src/tasks/auto_sfm/resize.py, matched to the actual source (not
a reconstruction). Mask handling has been dropped: the orchestrator never
stages masks (see staging.py), and autosfm.use_masking is expected to be
false for this pipeline. If mask support is needed later, re-port
remove_missing_data() and the mask branch of resize_and_save() from the
original file.

bbot_version is also dropped: it was threaded through resize_and_save() /
save_resized_image() in the original but never actually used in the save
path shown in source, so there's nothing behavioral to preserve.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from math import ceil
from pathlib import Path

from PIL import Image, ImageFile

ImageFile.LOAD_TRUNCATED_IMAGES = True
Image.MAX_IMAGE_PIXELS = 200_000_000

log = logging.getLogger(__name__)


def resize_image(image_src: Path, scale: float):
    """Resize a single image based on the given scale. Returns (resized, original) or (None, None)."""
    assert 0.0 < scale <= 1.0, "scale should be between (0, 1]."
    try:
        with Image.open(image_src) as image:
            width, height = image.size
            scaled_width = int(ceil(width * scale))
            scaled_height = int(ceil(height * scale))
            resized_image = image.resize((scaled_width, scaled_height))
            return resized_image, image.copy()
    except (IOError, SyntaxError) as e:
        log.error(f"Bad file: {image_src}. Error: {e}")
        return None, None


def build_save_kwargs_from_source_image(image: Image.Image) -> dict:
    """Preserve the original raw EXIF blob and ICC profile."""
    kwargs = {"quality": 100}
    if "exif" in image.info:
        kwargs["exif"] = image.info["exif"]
    else:
        log.warning("EXIF data not found, resizing without EXIF data.")
    if "icc_profile" in image.info:
        kwargs["icc_profile"] = image.info["icc_profile"]
    else:
        log.warning("ICC profile not found, resizing without ICC profile.")
    return kwargs


def update_exif_dimension_tags(image_dst: Path, width: int, height: int) -> None:
    """Requires exiftool on PATH."""
    if shutil.which("exiftool") is None:
        log.warning("exiftool not found; skipping EXIF dimension tag updates for %s", image_dst)
        return
    try:
        subprocess.run(
            [
                "exiftool",
                "-overwrite_original",
                f"-ExifImageWidth={width}",
                f"-ExifImageHeight={height}",
                f"-PixelXDimension={width}",
                f"-PixelYDimension={height}",
                str(image_dst),
            ],
            check=True,
            text=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as e:
        log.error("Failed to update EXIF dimension tags for %s. Error: %s", image_dst, e.stderr)


def save_resized_image(resized_image, image, image_dst: Path, update_dimensions: bool = True) -> None:
    try:
        kwargs = build_save_kwargs_from_source_image(image)
        resized_image.save(image_dst, **kwargs)
        if update_dimensions:
            width, height = resized_image.size
            update_exif_dimension_tags(image_dst, width, height)
    except Exception as e:
        log.error(f"Error saving file: {image_dst}. Error: {e}")


def resize_and_save(data: dict) -> None:
    """No NFS-retry branch here: the orchestrator stages files locally via
    Globus before this runs, so PermissionError-on-NFS isn't a case this
    layer needs to handle (unlike the old LTS-mounted-path world)."""
    image_src = Path(data["image_src"])
    image_dst = Path(data["image_dst"])
    scale = data["scale"]

    try:
        resized_image, image = resize_image(image_src, scale)
    except Exception as e:
        log.error(f"Error resizing image {image_src}. Error: {e}")
        return

    if resized_image and image:
        save_resized_image(resized_image, image, image_dst)


def resize_photo_directory(images_dir: Path, save_dir: Path, scale: float, overwrite: bool = False) -> Path:
    """Downscale every jpg in images_dir into save_dir. Returns save_dir.

    Takes explicit paths only — no Hydra config, no LTS directory discovery.
    staging.py is responsible for making sure images_dir already holds
    exactly the images for this run.
    """
    save_dir.mkdir(parents=True, exist_ok=True)

    files = sorted(list(images_dir.glob("*.jpg")) + list(images_dir.glob("*.JPG")))
    num_files = len(files)
    if num_files == 0:
        log.warning(f"No images found in {images_dir}")
        return save_dir

    if save_dir.exists() and not overwrite:
        already_copied = list(save_dir.glob("*.jpg")) + list(save_dir.glob("*.JPG"))
        already_copied_names = {img.name for img in already_copied}
        num_already_copied = len(already_copied)

        if num_already_copied == num_files:
            log.debug(f"All images ({num_already_copied}) have already been resized.")
            return save_dir

        if num_already_copied < num_files:
            log.debug(
                f"{num_already_copied} images have already been resized, "
                f"{num_files - num_already_copied} images remaining."
            )
            files = [f for f in files if f.name not in already_copied_names]
    else:
        log.warning("Overwrite enabled; existing resized images will be regenerated.")

    data = [
        {"image_src": src, "image_dst": save_dir / src.name, "scale": scale}
        for src in files
    ]

    try:
        with ThreadPoolExecutor(max_workers=16) as executor:
            futures = {executor.submit(resize_and_save, item): item for item in data}
            for i, future in enumerate(as_completed(futures), 1):
                try:
                    future.result()
                except Exception as e:
                    log.error(f"An error occurred while resizing: {e}")
    except KeyboardInterrupt:
        log.info("Interrupted by user, terminating...")
    finally:
        log.info("Completed resizing images.")

    return save_dir