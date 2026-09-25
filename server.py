import logging
import os
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from flask import Flask, jsonify, request, send_file
from flask_cors import CORS


# --------------------------------------------------
# Configuration
# --------------------------------------------------

app = Flask(__name__)

# Change this to your actual frontend URL in production.
FRONTEND_ORIGIN = os.getenv(
    "FRONTEND_ORIGIN",
    "https://puneethacharya71.github.io",
)

CORS(
    app,
    resources={
        r"/rip": {
            "origins": FRONTEND_ORIGIN
        }
    },
)

app.config["MAX_CONTENT_LENGTH"] = 16 * 1024  # 16 KB JSON request limit

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

logger = logging.getLogger("ripr")


# --------------------------------------------------
# Helpers
# --------------------------------------------------

ALLOWED_FORMATS = {"mp4", "mp3"}

# Keep this list limited to services you actually intend
# to support.
ALLOWED_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "youtu.be",
    "m.youtube.com",
    "instagram.com",
    "www.instagram.com",
}


def validate_url(url: str) -> bool:
    """Basic URL validation."""
    try:
        parsed = urlparse(url)

        if parsed.scheme not in {"http", "https"}:
            return False

        if not parsed.netloc:
            return False

        hostname = parsed.hostname
        if not hostname:
            return False

        return hostname.lower() in ALLOWED_HOSTS

    except Exception:
        return False


def build_ydl_options(temp_dir: str, format_type: str) -> dict:
    """Create yt-dlp configuration."""

    output_template = os.path.join(
        temp_dir,
        "%(title).150s.%(ext)s",
    )

    common = {
        "outtmpl": output_template,

        # Don't print huge amounts of yt-dlp output.
        "quiet": True,
        "no_warnings": True,

        # Avoid overwriting files.
        "nooverwrites": True,

        # Safer filenames.
        "restrictfilenames": True,
    }

    if format_type == "mp3":
        common.update(
            {
                "format": "bestaudio/best",

                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "320",
                    }
                ],
            }
        )

    else:
        common.update(
            {
                # Prefer MP4-compatible streams.
                "format": (
                    "bestvideo[ext=mp4]+bestaudio[ext=m4a]"
                    "/best[ext=mp4]"
                    "/best"
                ),

                # Merge video + audio into MP4 when required.
                "merge_output_format": "mp4",
            }
        )

    return common


def find_downloaded_file(temp_dir: str, format_type: str) -> Path | None:
    """Find the resulting media file."""

    directory = Path(temp_dir)

    expected_extension = f".{format_type}"

    files = [
        file
        for file in directory.iterdir()
        if file.is_file()
        and file.suffix.lower() == expected_extension
    ]

    if not files:
        return None

    # Normally there should only be one.
    return files[0]


# --------------------------------------------------
# Routes
# --------------------------------------------------

@app.get("/health")
def health():
    """Simple health-check endpoint."""
    return jsonify(
        {
            "status": "ok",
            "service": "ripr",
        }
    )


@app.post("/rip")
def rip_media():
    """Download media and return it as a file."""

    # ----------------------------------------------
    # Validate JSON
    # ----------------------------------------------

    if not request.is_json:
        return jsonify(
            {"error": "Request must contain JSON"}
        ), 400

    data = request.get_json(silent=True)

    if not isinstance(data, dict):
        return jsonify(
            {"error": "Invalid JSON body"}
        ), 400

    # ----------------------------------------------
    # Read parameters
    # ----------------------------------------------

    url = str(data.get("url", "")).strip()
    format_type = str(
        data.get("format", "mp4")
    ).lower().strip()

    # ----------------------------------------------
    # Validate URL
    # ----------------------------------------------

    if not url:
        return jsonify(
            {"error": "No URL provided"}
        ), 400

    if len(url) > 2048:
        return jsonify(
            {"error": "URL is too long"}
        ), 400

    if not validate_url(url):
        return jsonify(
            {
                "error": (
                    "Unsupported or invalid URL. "
                    "Only supported media URLs are accepted."
                )
            }
        ), 400

    # ----------------------------------------------
    # Validate format
    # ----------------------------------------------

    if format_type not in ALLOWED_FORMATS:
        return jsonify(
            {
                "error": "Invalid format. Use 'mp4' or 'mp3'."
            }
        ), 400

    temp_dir = tempfile.mkdtemp(prefix="ripr_")

    logger.info(
        "Download requested: format=%s",
        format_type,
    )

    try:
        ydl_opts = build_ydl_options(
            temp_dir,
            format_type,
        )

        # ------------------------------------------
        # Download
        # ------------------------------------------

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(
                url,
                download=True,
            )

            if not info:
                raise RuntimeError(
                    "Unable to extract media information."
                )

        # ------------------------------------------
        # Locate output
        # ------------------------------------------

        output_file = find_downloaded_file(
            temp_dir,
            format_type,
        )

        if output_file is None:
            # Useful fallback if yt-dlp generated an
            # unexpected extension.
            files = [
                file
                for file in Path(temp_dir).iterdir()
                if file.is_file()
            ]

            if not files:
                raise RuntimeError(
                    "Download completed but no file was produced."
                )

            output_file = files[0]

        logger.info(
            "Download completed: %s",
            output_file.name,
        )

        # ------------------------------------------
        # Send file
        # ------------------------------------------

        response = send_file(
            output_file,
            as_attachment=True,
            download_name=output_file.name,
        )

        # Delete temporary files after response.
        @response.call_on_close
        def cleanup():
            try:
                shutil.rmtree(
                    temp_dir,
                    ignore_errors=True,
                )
                logger.info(
                    "Cleaned temporary directory."
                )
            except Exception:
                logger.exception(
                    "Failed to clean temporary directory."
                )

        return response

    except yt_dlp.utils.DownloadError as exc:
        logger.warning(
            "yt-dlp download error: %s",
            exc,
        )

        shutil.rmtree(
            temp_dir,
            ignore_errors=True,
        )

        return jsonify(
            {
                "error": (
                    "The media could not be downloaded. "
                    "The URL may be unavailable or unsupported."
                )
            }
        ), 400

    except Exception:
        logger.exception(
            "Unexpected error while processing download."
        )

        shutil.rmtree(
            temp_dir,
            ignore_errors=True,
        )

        return jsonify(
            {
                "error": (
                    "An unexpected server error occurred."
                )
            }
        ), 500


# --------------------------------------------------
# Application entry point
# --------------------------------------------------

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
    )
