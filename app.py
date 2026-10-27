import os
import sys
import io
import json
import uuid
import zipfile
import base64
import webbrowser
from threading import Timer
from flask import Flask, render_template, request, jsonify, send_file
from cleaner_engine import (
    inspect_image_bytes,
    clean_image_bytes,
    compute_output_relpath,
    process_directory_tree,
)

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

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INPUT_DIR = os.path.join(BASE_DIR, "input")
OUTPUT_DIR = os.path.join(BASE_DIR, "output")

os.makedirs(INPUT_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

app = Flask(__name__)
# Allow up to 4 GB for large batch uploads
app.config["MAX_CONTENT_LENGTH"] = 4096 * 1024 * 1024


@app.errorhandler(413)
def request_entity_too_large(error):
    return jsonify({
        "success": False,
        "error": "حجم الملفات المرفوعة كبير جداً. يرجى استخدام زر 'معالجة المجلد بالكامل' مباشرة أو رفع الصور على دفعات أصغر."
    }), 413


@app.errorhandler(500)
def internal_server_error(error):
    return jsonify({
        "success": False,
        "error": f"حدث خطأ داخلي في الخادم: {str(error)}"
    }), 500

# In-memory cache of most recent batch for quick ZIP download
RECENT_BATCH = {}


def _resolve_target_format(requested_fmt: str, filename: str) -> str:
    req = (requested_fmt or "ORIGINAL").upper()
    if req in ("JPEG", "JPG"):
        return "JPEG"
    if req == "PNG":
        return "PNG"
    if req == "WEBP":
        return "WEBP"
    # ORIGINAL: default WebP from ChatGPT to JPEG or PNG?
    # Note: Exporting ChatGPT WebP/PNG as high-quality JPEG or clean PNG is great,
    # if ORIGINAL is chosen, keep PNG as PNG and convert WEBP/JPG to JPEG (or keep WEBP if requested).
    ext = os.path.splitext(filename)[1].lower()
    if ext == ".png":
        return "PNG"
    elif ext == ".webp":
        return "JPEG"  # Converting ChatGPT WebP to clean JPEG is standard for Facebook/Social Media
    return "JPEG"


@app.route("/favicon.ico")
def favicon():
    return ("", 204)


@app.route("/")
def index():
    return render_template("index.html", input_dir=INPUT_DIR, output_dir=OUTPUT_DIR)


@app.route("/api/inspect", methods=["POST"])
def api_inspect():
    files = request.files.getlist("images")
    results = []
    for f in files:
        if not f.filename:
            continue
        raw = f.read()
        report = inspect_image_bytes(raw, f.filename)
        report["filename"] = f.filename
        results.append(report)
    return jsonify({"success": True, "results": results})


@app.route("/api/process", methods=["POST"])
def api_process():
    files = request.files.getlist("images")
    if not files:
        return jsonify({"success": False, "error": "لم يتم اختيار أي صور للمعالجة."}), 400

    crop_percent = float(request.form.get("crop_percent", 0.0))
    keep_dims = request.form.get("keep_original_dimensions", "true").lower() == "true"
    pixel_shield = request.form.get("pixel_shield", "true").lower() == "true"
    output_format = request.form.get("output_format", "JPEG")
    quality = int(request.form.get("quality", 96))
    camera_profile = request.form.get("camera_profile", "none")
    auto_save = request.form.get("auto_save", "true").lower() == "true"
    folder_mode = request.form.get("folder_mode", "subfolder_clean")
    folder_suffix = request.form.get("folder_suffix", "_clean")

    # Relative paths can be passed as JSON array string
    relative_paths_raw = request.form.get("relative_paths")
    relative_paths = []
    if relative_paths_raw:
        try:
            relative_paths = json.loads(relative_paths_raw)
        except Exception:
            relative_paths = []

    batch_id = str(uuid.uuid4())[:8]
    RECENT_BATCH[batch_id] = []
    processed_items = []

    ext_map = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}
    mime_map = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}

    for idx, f in enumerate(files):
        if not f.filename:
            continue
        raw = f.read()
        target_fmt = _resolve_target_format(output_format, f.filename)

        cleaned_bytes, report = clean_image_bytes(
            raw,
            crop_percent=crop_percent,
            keep_original_dimensions=keep_dims,
            pixel_shield=pixel_shield,
            output_format=target_fmt,
            quality=quality,
            camera_profile=camera_profile,
        )

        base_name = os.path.splitext(os.path.basename(f.filename))[0]
        out_ext = ext_map.get(target_fmt, ".jpg")
        out_filename = f"{base_name}_clean{out_ext}"

        # Resolve relative path
        rel_path = relative_paths[idx] if (idx < len(relative_paths) and relative_paths[idx]) else f.filename
        out_relpath = compute_output_relpath(
            rel_path=rel_path,
            output_filename=out_filename,
            folder_mode=folder_mode,
            folder_suffix=folder_suffix
        )

        saved_path = None
        if auto_save:
            saved_path = os.path.join(OUTPUT_DIR, out_relpath)
            os.makedirs(os.path.dirname(saved_path), exist_ok=True)
            with open(saved_path, "wb") as out_f:
                out_f.write(cleaned_bytes)

        RECENT_BATCH[batch_id].append((out_relpath.replace("\\", "/"), cleaned_bytes))

        b64_preview = base64.b64encode(cleaned_bytes).decode("ascii")
        mime_type = mime_map.get(target_fmt, "image/jpeg")

        parent_folder = os.path.dirname(out_relpath).replace("\\", "/") or "المجلد الرئيسي"

        processed_items.append({
            "original_name": f.filename,
            "original_rel": rel_path,
            "output_name": out_filename,
            "output_rel": out_relpath.replace("\\", "/"),
            "parent_folder": parent_folder,
            "saved_path": saved_path,
            "preview_data_url": f"data:{mime_type};base64,{b64_preview}",
            "report": report,
        })

    return jsonify({
        "success": True,
        "batch_id": batch_id,
        "output_dir": OUTPUT_DIR,
        "items": processed_items,
    })


@app.route("/api/process-folder", methods=["POST"])
def api_process_folder():
    data = request.get_json(silent=True) or {}
    crop_percent = float(data.get("crop_percent", 0.0))
    keep_dims = bool(data.get("keep_original_dimensions", True))
    pixel_shield = bool(data.get("pixel_shield", True))
    output_format = data.get("output_format", "JPEG")
    quality = int(data.get("quality", 96))
    camera_profile = data.get("camera_profile", "none")
    folder_mode = data.get("folder_mode", "subfolder_clean")
    folder_suffix = data.get("folder_suffix", "_clean")
    custom_input = data.get("input_path", "").strip()
    custom_output = data.get("output_path", "").strip()

    target_input = custom_input if custom_input and os.path.exists(custom_input) else INPUT_DIR
    target_output = custom_output if custom_output else OUTPUT_DIR

    try:
        raw_items = process_directory_tree(
            input_dir=target_input,
            output_dir=target_output,
            crop_percent=crop_percent,
            keep_original_dimensions=keep_dims,
            pixel_shield=pixel_shield,
            output_format=output_format,
            quality=quality,
            camera_profile=camera_profile,
            folder_mode=folder_mode,
            folder_suffix=folder_suffix,
        )
    except Exception as e:
        return jsonify({"success": False, "error": f"خطأ أثناء معالجة المجلد: {str(e)}"}), 400

    if not raw_items:
        return jsonify({
            "success": False,
            "error": f"لم يتم العثور على أي صور داخل: {target_input} (أو المجلدات الفرعية)"
        }), 400

    batch_id = str(uuid.uuid4())[:8]
    RECENT_BATCH[batch_id] = []
    processed_items = []

    mime_map = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}

    for item in raw_items:
        rel_key = item["output_rel"].replace("\\", "/")
        RECENT_BATCH[batch_id].append((rel_key, item["cleaned_bytes"]))

        fmt = item["report"]["output_format"]
        mime_type = mime_map.get(fmt, "image/jpeg")
        b64_preview = base64.b64encode(item["cleaned_bytes"]).decode("ascii")

        processed_items.append({
            "original_name": os.path.basename(item["original_path"]),
            "original_rel": item["original_rel"].replace("\\", "/"),
            "output_name": item["filename"],
            "output_rel": rel_key,
            "parent_folder": item["parent_folder"],
            "saved_path": item["output_path"],
            "preview_data_url": f"data:{mime_type};base64,{b64_preview}",
            "report": item["report"],
        })

    return jsonify({
        "success": True,
        "batch_id": batch_id,
        "input_dir": target_input,
        "output_dir": target_output,
        "items": processed_items,
    })


@app.route("/api/check-folder", methods=["POST"])
def api_check_folder():
    data = request.get_json(silent=True) or {}
    custom_input = data.get("input_path", "").strip()
    target_input = custom_input if custom_input and os.path.exists(custom_input) else INPUT_DIR

    supported_exts = (".jpg", ".jpeg", ".png", ".webp", ".bmp")
    folder_counts = {}
    total_images = 0

    if os.path.exists(target_input):
        for root, _, files in os.walk(target_input):
            imgs = [f for f in files if f.lower().endswith(supported_exts)]
            if imgs:
                rel = os.path.relpath(root, target_input)
                folder_name = "المجلد الرئيسي" if rel == "." else rel
                folder_counts[folder_name] = len(imgs)
                total_images += len(imgs)

    return jsonify({
        "success": True,
        "input_path": target_input,
        "exists": os.path.exists(target_input),
        "total_images": total_images,
        "folder_counts": folder_counts,
    })


@app.route("/api/download-zip/<batch_id>", methods=["GET"])
def api_download_zip(batch_id: str):
    items = RECENT_BATCH.get(batch_id)
    if not items:
        return "Batch not found or expired", 404

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for fname, file_bytes in items:
            zf.writestr(fname, file_bytes)
    zip_buf.seek(0)

    return send_file(
        zip_buf,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"cleaned_images_{batch_id}.zip",
    )


@app.route("/api/open-folder", methods=["POST"])
def api_open_folder():
    data = request.get_json(silent=True) or {}
    folder_type = data.get("folder", "output")
    target = OUTPUT_DIR if folder_type == "output" else INPUT_DIR
    os.makedirs(target, exist_ok=True)
    try:
        os.startfile(target)
        return jsonify({"success": True, "path": target})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


if __name__ == "__main__":
    port = 5050
    url = f"http://127.0.0.1:{port}"
    print("=" * 60)
    print(f" AI Image Fingerprint Cleaner is running at: {url}")
    print(f" Input Folder : {INPUT_DIR}")
    print(f" Output Folder: {OUTPUT_DIR}")
    print("=" * 60)
    Timer(1.2, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=port, debug=False)
