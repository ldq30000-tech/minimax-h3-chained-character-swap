"""Small read-only routes used by the final video preview widget."""

from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    import folder_paths
    from aiohttp import web
    from server import PromptServer
except ImportError:  # Allows the node package to be imported outside ComfyUI.
    folder_paths = None
    web = None
    PromptServer = None


_ROUTES_REGISTERED = False


def _preview_item(path: Path, output_root: Path) -> dict[str, str]:
    relative = path.resolve().relative_to(output_root.resolve())
    return {
        "filename": relative.name,
        "subfolder": relative.parent.as_posix() if relative.parent != Path(".") else "",
        "type": "output",
    }


def _latest_final_video(filename_prefix: str = "") -> tuple[Path, Path] | None:
    if folder_paths is None:
        return None
    output_root = Path(folder_paths.get_output_directory()).resolve()
    safe_prefix = "".join(
        character
        for character in str(filename_prefix or "")
        if character.isalnum() or character in "-_."
    ).strip("._")
    pattern = f"{safe_prefix}*.mp4" if safe_prefix else "character_swap_full_exact*.mp4"
    candidates = [
        path
        for path in output_root.glob(f"h3_chains/*/final/{pattern}")
        if path.is_file()
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda item: item.stat().st_mtime_ns), output_root


def register_routes() -> None:
    global _ROUTES_REGISTERED
    if _ROUTES_REGISTERED or PromptServer is None or web is None:
        return
    prompt_server = getattr(PromptServer, "instance", None)
    routes = getattr(prompt_server, "routes", None)
    if routes is None:
        return

    @routes.get("/h3-chained-character-swap/latest-final")
    async def latest_final(request: Any) -> Any:
        found = _latest_final_video(request.query.get("filename_prefix", ""))
        if found is None:
            return web.json_response({"error": "no matching final video"}, status=404)
        path, output_root = found
        return web.json_response(
            {
                "video": _preview_item(path, output_root),
                "status": f"已恢复最近生成的完整视频：{path.name}",
                "size_bytes": path.stat().st_size,
            }
        )

    _ROUTES_REGISTERED = True
