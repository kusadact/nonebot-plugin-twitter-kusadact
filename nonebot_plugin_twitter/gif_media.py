"""Convert X's MP4-backed GIFs to looping image attachments for OneBot."""

import asyncio
from hashlib import sha256
from pathlib import Path
import shutil
from uuid import uuid4


GIF_VERSION = "1"


def gif_cache_path(url: str, cache_dir: Path) -> Path:
    key = sha256(f"{GIF_VERSION}:{url}".encode()).hexdigest()
    return Path(cache_dir) / f"{key}.gif"


async def convert_gif(source: Path, target: Path) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("gif_ffmpeg_unavailable")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(f".{uuid4().hex}.tmp.gif")
    process = await asyncio.create_subprocess_exec(
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
        "-filter_complex",
        "fps=15,scale=w='min(640,iw)':h=-1:flags=lanczos,split[a][b];"
        "[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=sierra2_4a",
        "-an", "-loop", "0", str(temporary),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await asyncio.wait_for(process.wait(), timeout=240)
        if process.returncode or not temporary.is_file() or temporary.stat().st_size == 0:
            raise RuntimeError("gif_conversion_failed")
        temporary.replace(target)
        return target
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        temporary.unlink(missing_ok=True)
