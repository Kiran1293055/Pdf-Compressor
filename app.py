from flask import Flask, render_template, request, send_file
import fitz
from PIL import Image
import io, os, tempfile, subprocess

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024  # 100 MB upload limit

def optimize_with_pymupdf(input_bytes, target_bytes):
    """Losslessly optimize first, then progressively downsample/re-encode embedded images."""
    doc = fitz.open(stream=input_bytes, filetype="pdf")
    best = doc.tobytes(garbage=4, deflate=True, deflate_fonts=True, deflate_images=True)
    doc.close()
    if len(best) <= target_bytes:
        return best

    # Try increasingly aggressive image settings. Existing vector/text content is retained.
    attempts = [
        (1600, 82), (1400, 76), (1200, 70), (1000, 64),
        (850, 58), (700, 52), (550, 45), (400, 38)
    ]
    for max_side, quality in attempts:
        try:
            doc = fitz.open(stream=input_bytes, filetype="pdf")
            seen = set()
            for page in doc:
                for info in page.get_images(full=True):
                    xref = info[0]
                    if xref in seen:
                        continue
                    seen.add(xref)
                    try:
                        extracted = doc.extract_image(xref)
                        raw = extracted.get("image")
                        if not raw:
                            continue
                        im = Image.open(io.BytesIO(raw))
                        if im.mode not in ("RGB", "L"):
                            # Keep transparency by skipping images where JPEG would damage appearance.
                            if im.mode in ("RGBA", "LA"):
                                bg = Image.new("RGB", im.size, "white")
                                bg.paste(im, mask=im.getchannel("A"))
                                im = bg
                            else:
                                im = im.convert("RGB")
                        im.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
                        buf = io.BytesIO()
                        if im.mode == "L":
                            im.save(buf, format="JPEG", quality=quality, optimize=True)
                        else:
                            im.convert("RGB").save(buf, format="JPEG", quality=quality, optimize=True)
                        if buf.tell() > 0:
                            doc.replace_image(xref, stream=buf.getvalue())
                    except Exception:
                        # Unsupported or unusual embedded image: leave it unchanged.
                        continue
            result = doc.tobytes(garbage=4, deflate=True, deflate_fonts=True, deflate_images=True)
            doc.close()
            if len(result) < len(best):
                best = result
            if len(best) <= target_bytes:
                return best
        except Exception:
            try:
                doc.close()
            except Exception:
                pass
            continue
    return best

def try_ghostscript(input_bytes, target_bytes):
    """Use Ghostscript when installed; otherwise return None."""
    gs = None
    for candidate in ("gs", "gswin64c", "gswin32c"):
        try:
            subprocess.run([candidate, "--version"], capture_output=True, timeout=5, check=True)
            gs = candidate
            break
        except Exception:
            continue
    if not gs:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "input.pdf")
        dst = os.path.join(tmp, "output.pdf")
        with open(src, "wb") as f:
            f.write(input_bytes)
        # The loop tries progressively smaller output presets.
        for setting in ("/printer", "/ebook", "/screen"):
            cmd = [gs, "-sDEVICE=pdfwrite", "-dCompatibilityLevel=1.4",
                   "-dNOPAUSE", "-dBATCH", "-dQUIET",
                   "-dPDFSETTINGS=" + setting, "-sOutputFile=" + dst, src]
            try:
                subprocess.run(cmd, capture_output=True, timeout=90, check=True)
                with open(dst, "rb") as f:
                    data = f.read()
                if len(data) <= target_bytes:
                    return data
            except Exception:
                continue
        try:
            with open(dst, "rb") as f:
                return f.read()
        except Exception:
            return None

@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")

@app.route("/compress", methods=["POST"])
def compress():
    upload = request.files.get("pdf")
    target_kb = request.form.get("target_kb", "20")
    if not upload or not upload.filename:
        return render_template("index.html", error="Please choose a PDF file.")
    if not upload.filename.lower().endswith(".pdf"):
        return render_template("index.html", error="Only PDF files are supported.")
    try:
        target_kb = float(target_kb)
        if target_kb < 1 or target_kb > 500000:
            raise ValueError()
    except ValueError:
        return render_template("index.html", error="Enter a target size between 1 KB and 500,000 KB.")

    original = upload.read()
    if not original:
        return render_template("index.html", error="The uploaded file is empty.")
    try:
        # Validate PDF before processing.
        test = fitz.open(stream=original, filetype="pdf")
        if test.needs_pass:
            test.close()
            return render_template("index.html", error="This PDF is password-protected. Unlock it before compressing.")
        test.close()
        target_bytes = int(target_kb * 1024)
        optimized = optimize_with_pymupdf(original, target_bytes)
        gs_result = try_ghostscript(original, target_bytes)
        if gs_result and len(gs_result) < len(optimized):
            optimized = gs_result

        achieved = len(optimized)
        original_size = len(original)
        reduction = max(0, (1 - achieved / original_size) * 100)
        output_name = os.path.splitext(os.path.basename(upload.filename))[0] + "_compressed.pdf"
        note = None
        if achieved > target_bytes:
            note = (f"The requested target ({target_kb:g} KB) could not be reached without more destructive "
                    f"changes. The smallest result found was {achieved / 1024:.1f} KB. "
                    "PDFs containing mostly text, vector graphics, or already-compressed images may not shrink further.")
        return send_file(io.BytesIO(optimized), mimetype="application/pdf",
                         as_attachment=True, download_name=output_name,
                         etag=False)
    except Exception as exc:
        app.logger.exception("PDF compression failed")
        return render_template("index.html", error="Could not process this PDF. It may be damaged or use unsupported features.")

@app.errorhandler(413)
def too_large(_):
    return render_template("index.html", error="File is too large. Maximum upload size is 100 MB."), 413

if __name__ == "__main__":
    app.run(debug=True)
