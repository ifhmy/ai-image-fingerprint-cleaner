import io
import os
import sys
import struct
import random
from datetime import datetime, timedelta
from typing import Dict, Any, List, Tuple, Optional
import numpy as np
from PIL import Image, ImageOps, ImageEnhance

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


AI_KEYWORDS = [
    b"c2pa",
    b"caBX",
    b"jumb",
    b"JUMBF",
    b"contentauth",
    b"OpenAI",
    b"openai",
    b"DALL-E",
    b"DALL\xc2\xb7E",
    b"dalle",
    b"ChatGPT",
    b"GPT-4o",
    b"trainedAlgorithmicMedia",
    b"compositeWithTrainedAlgorithmicMedia",
    b"algorithmicMedia",
    b"DigitalSourceType",
    b"digitalsourcetype",
    b"Adobe Firefly",
    b"Firefly",
    b"Midjourney",
    b"Stable Diffusion",
    b"ComfyUI",
    b"Automatic1111",
    b"SynthID",
    b"bing.com",
    b"Image Creator",
]


def _scan_png_chunks(data: bytes) -> List[str]:
    """Scan PNG binary chunks for metadata and C2PA blocks."""
    findings = []
    if len(data) < 8 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return findings

    pos = 8
    essential_chunks = {b"IHDR", b"PLTE", b"IDAT", b"IEND", b"tRNS", b"sRGB", b"gAMA", b"pHYs"}
    while pos + 8 <= len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        chunk_type = data[pos + 4:pos + 8]
        chunk_data = data[pos + 8:pos + 8 + length]

        if chunk_type == b"caBX" or chunk_type == b"jumb":
            findings.append("بصمة C2PA Content Credentials (صندوق caBX/JUMBF الخاص بـ OpenAI/ChatGPT)")
        elif chunk_type == b"eXIf":
            findings.append("بيانات EXIF مدمجة داخل PNG (eXIf)")
        elif chunk_type in (b"tEXt", b"iTXt", b"zTXt"):
            text_preview = chunk_data[:200].decode("utf-8", errors="ignore")
            if "XML:com.adobe.xmp" in text_preview or "xmp" in text_preview.lower():
                findings.append("حزمة بيانات XMP (تحتوي على سجل المصدر الرقمي)")
            else:
                findings.append(f"نصوص وصفية مخفية داخل PNG ({chunk_type.decode('ascii', errors='ignore')})")
        elif chunk_type == b"iCCP":
            findings.append("ملف تعريف لوني iCCP (قد يحمل توقيع البرنامج المُنشئ)")
        elif chunk_type not in essential_chunks:
            findings.append(f"قطاع بيانات إضافي ({chunk_type.decode('ascii', errors='ignore')})")

        pos += 12 + length
        if chunk_type == b"IEND":
            break

    return findings


def _scan_jpeg_markers(data: bytes) -> List[str]:
    """Scan JPEG binary markers for APP1 (EXIF/XMP), APP11 (C2PA/JUMBF), APP13 (IPTC), COM."""
    findings = []
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return findings

    pos = 2
    while pos + 4 <= len(data):
        if data[pos] != 0xFF:
            break
        marker = data[pos + 1]
        # SOS (Start of Scan) or EOI (End of Image)
        if marker == 0xDA or marker == 0xD9:
            break
        # Standalone markers without length
        if marker in (0xD8, 0x01) or (0xD0 <= marker <= 0xD7):
            pos += 2
            continue

        length = struct.unpack(">H", data[pos + 2:pos + 4])[0]
        segment = data[pos + 4:pos + 2 + length]

        if marker == 0xE1:  # APP1
            if segment.startswith(b"Exif"):
                findings.append("بيانات EXIF وصفية (APP1 Exif)")
            elif b"http://ns.adobe.com/xap" in segment or b"xmp" in segment.lower():
                findings.append("حزمة بيانات XMP (APP1 XMP)")
            else:
                findings.append("قطاع بيانات APP1 إضافي")
        elif marker == 0xEB:  # APP11 - JUMBF / C2PA
            findings.append("بصمة C2PA / JUMBF الخاصة بالذكاء الاصطناعي (APP11)")
        elif marker == 0xED:  # APP13 - Photoshop / IPTC
            findings.append("بيانات IPTC / Photoshop (APP13 DigitalSourceType)")
        elif marker == 0xE2:  # APP2 - ICC
            if b"ICC_PROFILE" not in segment:
                findings.append("قطاع بيانات APP2")
        elif marker == 0xFE:  # COM
            findings.append("تعليق مخفي داخل JPEG (COM Marker)")

        pos += 2 + length

    return findings


def _scan_webp_chunks(data: bytes) -> List[str]:
    """Scan WebP RIFF container chunks for C2PA, EXIF, XMP."""
    findings = []
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return findings

    pos = 12
    while pos + 8 <= len(data):
        chunk_fourcc = data[pos:pos + 4]
        chunk_size = struct.unpack("<I", data[pos + 4:pos + 8])[0]
        if chunk_fourcc in (b"C2PA", b"JUMB", b"jumb", b"caBX"):
            findings.append("بصمة C2PA Content Credentials مدمجة داخل WebP (خاص بـ ChatGPT)")
        elif chunk_fourcc == b"EXIF":
            findings.append("بيانات EXIF داخل ملف WebP")
        elif chunk_fourcc in (b"XMP ", b"XMP\x00"):
            findings.append("حزمة بيانات XMP داخل ملف WebP")
        # RIFF chunks are padded to even byte boundaries
        pos += 8 + chunk_size + (chunk_size % 2)

    return findings


def inspect_image_bytes(data: bytes, filename: str = "") -> Dict[str, Any]:
    """
    Inspect raw image bytes for AI watermarks/metadata (C2PA, JUMBF, XMP DigitalSourceType, EXIF, etc.).
    """
    findings: List[str] = []
    ai_markers_found: List[str] = []

    # 1. Container-specific structure scan
    findings.extend(_scan_png_chunks(data))
    findings.extend(_scan_jpeg_markers(data))
    findings.extend(_scan_webp_chunks(data))

    # 2. Binary signature scan for explicit AI generator tags
    for kw in AI_KEYWORDS:
        if kw in data:
            kw_str = kw.decode("utf-8", errors="ignore")
            ai_markers_found.append(kw_str)

    # 3. Pillow info / EXIF inspection
    width, height, fmt, mode = 0, 0, "UNKNOWN", "RGB"
    try:
        with Image.open(io.BytesIO(data)) as img:
            width, height = img.size
            fmt = img.format or "UNKNOWN"
            mode = img.mode
            info_keys = [k for k in img.info.keys() if k not in ("jfif", "jfif_version", "jfif_unit", "jfif_density", "dpi", "progression")]
            for k in info_keys:
                if k.lower() in ("exif", "xmp", "xml:com.adobe.xmp", "c2pa", "parameters", "prompt", "workflow", "comment", "software", "description"):
                    label = f"حقل بيانات ميتاداتا: {k}"
                    if label not in findings:
                        findings.append(label)
            exif_obj = img.getexif()
            if exif_obj and len(exif_obj) > 0:
                label = f"سجل EXIF يحتوي على {len(exif_obj)} وسم"
                if not any("EXIF" in f for f in findings):
                    findings.append(label)
    except Exception:
        pass

    # Deduplicate findings
    unique_findings = list(dict.fromkeys(findings))
    unique_ai = list(dict.fromkeys(ai_markers_found))

    # Categorize into direct AI triggers vs general metadata
    ai_reasons = []
    general_meta = []

    for f in unique_findings:
        if any(term in f for term in ("C2PA", "JUMBF", "caBX", "XMP", "الذكاء الاصطناعي", "OpenAI", "ChatGPT")):
            ai_reasons.append(f)
        else:
            general_meta.append(f)

    for sig in unique_ai:
        if sig in ("c2pa", "caBX", "jumb", "JUMBF", "contentauth"):
            ai_reasons.append(f"بصمة توثيق المحتوى C2PA ({sig})")
        elif "algorithmic" in sig.lower() or "digitalsourcetype" in sig.lower():
            ai_reasons.append(f"وسم تصنيف الذكاء الاصطناعي IPTC/XMP ({sig}) - السبب الرئيسي لتنبيه فيسبوك")
        elif any(brand in sig.lower() for brand in ("openai", "dall", "chatgpt", "gpt-4o")):
            ai_reasons.append(f"توقيع OpenAI / ChatGPT الصريح ({sig})")
        else:
            ai_reasons.append(f"توقيع أداة ذكاء اصطناعي ({sig})")

    ai_reasons = list(dict.fromkeys(ai_reasons))
    general_meta = list(dict.fromkeys(general_meta))

    has_ai = len(ai_reasons) > 0
    has_meta = len(general_meta) > 0

    if has_ai:
        status = "AI_DETECTED"
        status_label = "تم العثور على بصمة AI مكشوفة لفيسبوك ⚠️"
    elif has_meta:
        status = "METADATA_FOUND"
        status_label = "تحتوي على بيانات وصفية عادية (بدون بصمة AI صريحة)"
    else:
        status = "CLEAN"
        status_label = "نظيفة 100% (خالية من أي بصمة أو بيانات)"

    return {
        "status": status,
        "status_label": status_label,
        "has_ai_fingerprint": has_ai,
        "has_metadata": has_meta or has_ai,
        "ai_reasons": ai_reasons,
        "general_metadata": general_meta,
        "findings": unique_findings,
        "ai_signatures": unique_ai,
        "width": width,
        "height": height,
        "format": fmt,
        "size_bytes": len(data),
    }


def _build_camera_exif(profile: str, width: int, height: int) -> Optional[bytes]:
    """
    Build realistic camera EXIF metadata when requested by user.
    Uses Pillow's native Image.Exif() without external dependencies.
    """
    if not profile or profile == "none":
        return None

    profiles = {
        "iphone": {
            "Make": "Apple",
            "Model": "iPhone 16 Pro",
            "Software": "18.1",
            "LensMake": "Apple",
            "LensModel": "iPhone 16 Pro back triple camera 6.765mm f/1.78",
        },
        "samsung": {
            "Make": "Samsung",
            "Model": "SM-S928B",
            "Software": "S928BXXU3AXFE",
            "LensMake": "Samsung",
            "LensModel": "Samsung Galaxy S24 Ultra Rear Main Camera",
        },
        "canon": {
            "Make": "Canon",
            "Model": "Canon EOS R5",
            "Software": "Firmware Version 2.0.0",
            "LensMake": "Canon",
            "LensModel": "RF24-70mm F2.8 L IS USM",
        },
    }

    selected = profiles.get(profile)
    if not selected:
        return None

    exif = Image.Exif()
    now_str = (datetime.now() - timedelta(minutes=random.randint(5, 180))).strftime("%Y:%m:%d %H:%M:%S")

    # Standard TIFF/EXIF tags
    exif[0x010F] = selected["Make"]        # Make
    exif[0x0110] = selected["Model"]       # Model
    exif[0x0112] = 1                       # Orientation (Normal)
    exif[0x011A] = (72, 1)                 # XResolution
    exif[0x011B] = (72, 1)                 # YResolution
    exif[0x0128] = 2                       # ResolutionUnit (Inches)
    exif[0x0131] = selected["Software"]    # Software
    exif[0x0132] = now_str                 # DateTime
    exif[0x0213] = 1                       # YCbCrPositioning

    return exif.tobytes()


def clean_image_bytes(
    data: bytes,
    crop_percent: float = 1.0,
    keep_original_dimensions: bool = True,
    pixel_shield: bool = True,
    output_format: str = "JPEG",
    quality: int = 95,
    camera_profile: str = "none",
) -> Tuple[bytes, Dict[str, Any]]:
    """
    Completely strips all AI metadata (C2PA, JUMBF, XMP, EXIF, IPTC, chunks),
    rebuilds the image from a raw NumPy pixel buffer, optionally applies Canva-style
    smart micro-crop and imperceptible pixel/frequency perturbation, and exports a clean file.
    """
    before_report = inspect_image_bytes(data)

    with Image.open(io.BytesIO(data)) as src_img:
        # 1. Apply EXIF orientation before stripping EXIF so image doesn't rotate sideways
        src_img = ImageOps.exif_transpose(src_img)
        orig_w, orig_h = src_img.size

        # Handle transparency properly depending on target format
        target_fmt = output_format.upper()
        if target_fmt == "JPG":
            target_fmt = "JPEG"

        if target_fmt == "JPEG":
            if src_img.mode in ("RGBA", "LA") or (src_img.mode == "P" and "transparency" in src_img.info):
                rgba = src_img.convert("RGBA")
                background = Image.new("RGB", rgba.size, (255, 255, 255))
                background.paste(rgba, mask=rgba.split()[3])
                working_img = background
            else:
                working_img = src_img.convert("RGB")
        else:
            if src_img.mode not in ("RGB", "RGBA"):
                working_img = src_img.convert("RGBA" if "A" in src_img.mode else "RGB")
            else:
                working_img = src_img.copy()

        # 2. Rebuild from raw NumPy pixel array (100% decouples from original file container/chunks)
        raw_pixels = np.array(working_img, dtype=np.uint8, copy=True)

    # 3. Apply Imperceptible Pixel Shield (breaks frequency/DCT/StegaStamp/SynthID watermarks)
    if pixel_shield:
        arr_f = raw_pixels.astype(np.float32)
        channels = 3 if arr_f.ndim == 3 and arr_f.shape[2] >= 3 else 1

        # Subtle per-channel micro calibration (0.2% - 0.4% shift, completely invisible to human eye)
        if channels == 3:
            rgb = arr_f[:, :, :3]
            # Slight gamma & micro-warmth shift (similar to Canva color space rendering)
            scales = np.array([1.0025, 0.9990, 1.0015], dtype=np.float32)
            rgb = (rgb - 128.0) * scales + 128.0

            # Imperceptible natural sensor micro-dither (±0.6 intensity on 0-255 scale)
            rng = np.random.default_rng()
            noise = rng.normal(0.0, 0.45, size=rgb.shape).astype(np.float32)
            rgb = np.clip(rgb + noise, 0, 255)
            arr_f[:, :, :3] = rgb
        else:
            rng = np.random.default_rng()
            noise = rng.normal(0.0, 0.45, size=arr_f.shape).astype(np.float32)
            arr_f = np.clip(arr_f + noise, 0, 255)

        raw_pixels = np.rint(arr_f).astype(np.uint8)

    # Create brand-new clean PIL Image from raw array (Zero metadata, Zero info dict)
    clean_img = Image.fromarray(raw_pixels)

    # 4. Smart Micro-Crop (Canva-style spatial phase shift)
    crop_percent = max(0.0, min(float(crop_percent), 10.0))
    if crop_percent > 0.0 and orig_w > 32 and orig_h > 32:
        dx = max(1, int(round(orig_w * (crop_percent / 100.0) * 0.5)))
        dy = max(1, int(round(orig_h * (crop_percent / 100.0) * 0.5)))
        left = dx
        top = dy
        right = max(left + 16, orig_w - dx)
        bottom = max(top + 16, orig_h - dy)
        clean_img = clean_img.crop((left, top, right, bottom))

        if keep_original_dimensions:
            clean_img = clean_img.resize((orig_w, orig_h), Image.Resampling.LANCZOS)

    # Slight imperceptible sharpness normalization after Lanczos
    if pixel_shield:
        clean_img = ImageEnhance.Sharpness(clean_img).enhance(1.02)

    # 5. Export to clean byte stream with zero AI chunks
    out_buf = io.BytesIO()
    exif_bytes = _build_camera_exif(camera_profile, clean_img.width, clean_img.height)

    save_kwargs: Dict[str, Any] = {}
    if target_fmt == "JPEG":
        save_kwargs["quality"] = max(60, min(int(quality), 100))
        save_kwargs["subsampling"] = 0 if quality >= 92 else 1
        save_kwargs["optimize"] = True
        if exif_bytes:
            save_kwargs["exif"] = exif_bytes
    elif target_fmt == "PNG":
        save_kwargs["optimize"] = True
        if exif_bytes:
            save_kwargs["exif"] = exif_bytes
    elif target_fmt == "WEBP":
        save_kwargs["quality"] = max(60, min(int(quality), 100))
        save_kwargs["method"] = 4
        if exif_bytes:
            save_kwargs["exif"] = exif_bytes

    clean_img.save(out_buf, format=target_fmt, **save_kwargs)
    out_bytes = out_buf.getvalue()

    after_report = inspect_image_bytes(out_bytes)

    return out_bytes, {
        "before": before_report,
        "after": after_report,
        "output_width": clean_img.width,
        "output_height": clean_img.height,
        "output_format": target_fmt,
        "output_size_bytes": len(out_bytes),
    }


def compute_output_relpath(
    rel_path: str,
    output_filename: str,
    folder_mode: str = "subfolder_clean",
    folder_suffix: str = "_clean"
) -> str:
    """
    Computes relative output path preserving or creating new folder structure.
    rel_path: relative path of source file, e.g. 'Folder1/pic1.jpg' or 'pic1.jpg'
    output_filename: cleaned file name, e.g. 'pic1_clean.jpg'
    """
    normalized = rel_path.replace("\\", "/").strip("/")
    parts = normalized.split("/")[:-1] if "/" in normalized else []
    base_stem = os.path.splitext(output_filename)[0]
    suffix = folder_suffix if folder_suffix is not None else "_clean"

    if folder_mode == "subfolder_clean":
        if parts:
            new_parts = [f"{p}{suffix}" for p in parts]
            return os.path.join(*new_parts, output_filename)
        return output_filename

    elif folder_mode == "per_image":
        # Each image placed inside its own dedicated folder
        if parts:
            new_parts = [f"{p}{suffix}" for p in parts]
            return os.path.join(*new_parts, f"{base_stem}", output_filename)
        return os.path.join(f"{base_stem}", output_filename)

    elif folder_mode == "mirror":
        # Keep exact folder names
        if parts:
            return os.path.join(*parts, output_filename)
        return output_filename

    # Default fallback
    if parts:
        new_parts = [f"{p}{suffix}" for p in parts]
        return os.path.join(*new_parts, output_filename)
    return output_filename


def process_directory_tree(
    input_dir: str,
    output_dir: str,
    crop_percent: float = 0.0,
    keep_original_dimensions: bool = True,
    pixel_shield: bool = True,
    output_format: str = "JPEG",
    quality: int = 96,
    camera_profile: str = "none",
    folder_mode: str = "subfolder_clean",
    folder_suffix: str = "_clean",
    supported_exts: Tuple[str, ...] = (".jpg", ".jpeg", ".png", ".webp", ".bmp")
) -> List[Dict[str, Any]]:
    """
    Recursively scans input_dir and processes all images, placing them into
    corresponding subfolders with modified names (e.g. Folder1_clean).
    """
    if not os.path.exists(input_dir):
        raise FileNotFoundError(f"المجلد المحدد غير موجود: {input_dir}")

    os.makedirs(output_dir, exist_ok=True)
    ext_map = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}

    results = []
    # Collect all image files
    all_files = []
    for root, _, files in os.walk(input_dir):
        for fname in files:
            if fname.lower().endswith(supported_exts):
                full_src = os.path.join(root, fname)
                rel_path = os.path.relpath(full_src, input_dir)
                all_files.append((full_src, rel_path, fname))

    total_files = len(all_files)
    print(f"[*] تم العثور على {total_files} صورة في المجلدات. جاري المعالجة...")

    for idx, (full_src, rel_path, fname) in enumerate(all_files, 1):
        with open(full_src, "rb") as f:
            raw = f.read()

        base_name = os.path.splitext(fname)[0]
        orig_ext = os.path.splitext(fname)[1].lower()

        fmt = output_format.upper()
        if fmt == "ORIGINAL":
            if orig_ext == ".png":
                fmt = "PNG"
            elif orig_ext == ".webp":
                fmt = "WEBP"
            else:
                fmt = "JPEG"

        out_ext = ext_map.get(fmt, ".jpg")
        out_filename = f"{base_name}_clean{out_ext}"

        out_relpath = compute_output_relpath(
            rel_path=rel_path,
            output_filename=out_filename,
            folder_mode=folder_mode,
            folder_suffix=folder_suffix
        )
        full_dest = os.path.join(output_dir, out_relpath)
        os.makedirs(os.path.dirname(full_dest), exist_ok=True)

        cleaned_bytes, report = clean_image_bytes(
            raw,
            crop_percent=crop_percent,
            keep_original_dimensions=keep_original_dimensions,
            pixel_shield=pixel_shield,
            output_format=fmt,
            quality=quality,
            camera_profile=camera_profile,
        )

        with open(full_dest, "wb") as out_f:
            out_f.write(cleaned_bytes)

        parent_folder = os.path.dirname(out_relpath).replace("\\", "/") or "المجلد الرئيسي"

        if total_files > 0 and (idx % 5 == 0 or idx == total_files or idx <= 5):
            print(f"  [{idx}/{total_files}] تم تنظيف: {out_relpath}")

        results.append({
            "original_path": full_src,
            "original_rel": rel_path,
            "output_path": full_dest,
            "output_rel": out_relpath,
            "parent_folder": parent_folder,
            "filename": out_filename,
            "cleaned_bytes": cleaned_bytes,
            "report": report
        })

    return results


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Clean AI fingerprints and C2PA/EXIF/XMP metadata from images.")
    parser.add_argument("--input", "-i", default="input", help="Input file or directory")
    parser.add_argument("--output", "-o", default="output", help="Output directory")
    parser.add_argument("--crop", type=float, default=0.0, help="Smart micro-crop percentage (default: 0.0)")
    parser.add_argument("--format", default="JPEG", choices=["JPEG", "PNG", "WEBP", "ORIGINAL"], help="Output format")
    parser.add_argument("--quality", type=int, default=96, help="Output quality (60-100)")
    parser.add_argument("--camera", default="none", choices=["none", "iphone", "samsung", "canon"], help="Optional camera EXIF profile")
    parser.add_argument("--folder-mode", default="subfolder_clean", choices=["subfolder_clean", "per_image", "mirror"], help="Folder structure mode")
    parser.add_argument("--folder-suffix", default="_clean", help="Suffix for new output folders (default: _clean)")
    args = parser.parse_args()

    if os.path.isdir(args.input):
        print(f"[*] جاري فحص ومعالجة المجلدات والملفات داخل: {args.input}")
        items = process_directory_tree(
            input_dir=args.input,
            output_dir=args.output,
            crop_percent=args.crop,
            keep_original_dimensions=True,
            pixel_shield=True,
            output_format=args.format,
            quality=args.quality,
            camera_profile=args.camera,
            folder_mode=args.folder_mode,
            folder_suffix=args.folder_suffix
        )
        print(f"[+] اكتملت المعالجة! تم تنظيف {len(items)} صورة وتوزيعها في مجلدات جديدة داخل: {args.output}")
        for item in items:
            print(f"  -> {item['output_rel']}")
    elif os.path.isfile(args.input):
        os.makedirs(args.output, exist_ok=True)
        with open(args.input, "rb") as f:
            raw = f.read()
        base_name = os.path.splitext(os.path.basename(args.input))[0]
        ext_map = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}
        fmt = "JPEG" if args.format == "ORIGINAL" else args.format
        out_name = f"{base_name}_clean{ext_map.get(fmt, '.jpg')}"
        out_path = os.path.join(args.output, out_name)
        cleaned_bytes, report = clean_image_bytes(
            raw,
            crop_percent=args.crop,
            keep_original_dimensions=True,
            pixel_shield=True,
            output_format=fmt,
            quality=args.quality,
            camera_profile=args.camera
        )
        with open(out_path, "wb") as f:
            f.write(cleaned_bytes)
        print(f"[+] تم تنظيف وحفظ الصورة: {out_path}")
    else:
        print(f"[!] المسار غير موجود: {args.input}")
