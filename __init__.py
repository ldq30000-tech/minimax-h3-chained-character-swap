"""ComfyUI entry points for the guarded MiniMax H3 chain runner."""

from .nodes import (
    H3ChainConfig,
    H3ChainLaunch,
    H3ChainStatus,
    H3FinalTrimToSource,
    H3FullVideoConfig,
    H3FullVideoDiagnostics,
    H3FullVideoInputs,
    H3FullVideoLaunch,
    H3FullVideoOneClick,
    H3FullVideoStatus,
    H3NativeGenerationFingerprint,
    H3NativeLongVideoPrepare,
    H3NativeLongVideoScene,
)
from .server_routes import register_routes

register_routes()


NODE_CLASS_MAPPINGS = {
    "H3ChainConfig": H3ChainConfig,
    "H3ChainLaunch": H3ChainLaunch,
    "H3ChainStatus": H3ChainStatus,
    "H3NativeGenerationFingerprint": H3NativeGenerationFingerprint,
    "H3NativeLongVideoPrepare": H3NativeLongVideoPrepare,
    "H3NativeLongVideoScene": H3NativeLongVideoScene,
    "H3FinalTrimToSource": H3FinalTrimToSource,
    "H3FullVideoInputs": H3FullVideoInputs,
    "H3FullVideoConfig": H3FullVideoConfig,
    "H3FullVideoDiagnostics": H3FullVideoDiagnostics,
    "H3FullVideoLaunch": H3FullVideoLaunch,
    "H3FullVideoOneClick": H3FullVideoOneClick,
    "H3FullVideoStatus": H3FullVideoStatus,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "H3ChainConfig": "H3 Chain Config",
    "H3ChainLaunch": "H3 Chain Launch",
    "H3ChainStatus": "H3 Chain Status",
    "H3NativeGenerationFingerprint": "H3 Native Identity + Source Fingerprint",
    "H3NativeLongVideoPrepare": "H3 Native Long Video Stream Plan",
    "H3NativeLongVideoScene": "H3 Native Stream Current Scene",
    "H3FinalTrimToSource": "H3 Final Exact Trim + Source Audio",
    "H3FullVideoInputs": "H3 Full Video Inputs",
    "H3FullVideoConfig": "H3 Full Video Config",
    "H3FullVideoDiagnostics": "H3 Full Video Diagnostics",
    "H3FullVideoLaunch": "H3 Full Video Launch",
    "H3FullVideoOneClick": "H3 Full Video One Click",
    "H3FullVideoStatus": "H3 Full Video Status",
}

WEB_DIRECTORY = "./web"

__all__ = [
    "NODE_CLASS_MAPPINGS",
    "NODE_DISPLAY_NAME_MAPPINGS",
    "WEB_DIRECTORY",
]
