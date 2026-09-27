"""Native Gemini VIDEO transport, preserving ComfyUI audio and trims."""

from contextlib import contextmanager
from pathlib import Path
import math
import tempfile
import time


@contextmanager
def gemini_video_part(client, types, video, fps, timeout):
    """Export the logical clip, upload it, and release only our temporary files."""
    if not callable(getattr(video, "save_to", None)):
        raise ValueError(
            "Native Gemini video requires a ComfyUI VIDEO object. Connect Create Video "
            "(images + audio + frame rate), or choose sampled_frames for an image batch."
        )
    fps = float(fps)
    if not math.isfinite(fps) or not 0 < fps <= 24:
        raise ValueError("gemini_video_fps must be greater than 0 and at most 24.")
    # Validate SDK support before exporting or uploading anything.
    metadata = types.VideoMetadata(fps=fps)
    uploaded = None
    try:
        with tempfile.TemporaryDirectory(prefix="llm_prompt_video_") as directory:
            path = Path(directory) / "reference.mp4"
            # save_to honours trimmed VideoFromFile objects and muxes the audio
            # in VideoFromComponents. Reading the raw stream would bypass trims.
            video.save_to(str(path), format="mp4", codec="h264")
            if not path.is_file() or path.stat().st_size == 0:
                raise RuntimeError("Video export produced an empty file.")
            uploaded = client.files.upload(
                file=str(path), config=types.UploadFileConfig(mime_type="video/mp4")
            )
        deadline = time.monotonic() + float(timeout)
        while True:
            state = getattr(uploaded.state, "name", uploaded.state)
            if state == "ACTIVE":
                break
            if state == "FAILED":
                raise RuntimeError("Gemini could not process the uploaded video.")
            if state not in (None, "PROCESSING", "STATE_UNSPECIFIED"):
                raise RuntimeError(f"Unexpected Gemini video state: {state!r}")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Timed out waiting for Gemini video processing.")
            time.sleep(min(2.0, remaining))
            uploaded = client.files.get(name=uploaded.name)
        if not uploaded.uri:
            raise RuntimeError("Gemini processed the video but returned no file URI.")
        yield types.Part(
            file_data=types.FileData(file_uri=uploaded.uri, mime_type="video/mp4"),
            video_metadata=metadata,
        )
    finally:
        if uploaded is not None and uploaded.name:
            try:
                client.files.delete(name=uploaded.name)
            except Exception:
                print("[LLM_Prompt_API] Temporary Gemini video cleanup failed; "
                      "the uploaded file will expire under Gemini Files retention.")
