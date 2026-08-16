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
    H3NativeLongVideoPrepare,
)


NODE_CLASS_MAPPINGS = {
    "H3ChainConfig": H3ChainConfig,
    "H3ChainLaunch": H3ChainLaunch,
    "H3ChainStatus": H3ChainStatus,
    "H3NativeLongVideoPrepare": H3NativeLongVideoPrepare,
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
    "H3NativeLongVideoPrepare": "H3 Native Long Video Plan + Pad",
    "H3FinalTrimToSource": "H3 Final Exact Trim + Source Audio",
    "H3FullVideoInputs": "H3 Full Video Inputs",
    "H3FullVideoConfig": "H3 Full Video Config",
    "H3FullVideoDiagnostics": "H3 Full Video Diagnostics",
    "H3FullVideoLaunch": "H3 Full Video Launch",
    "H3FullVideoOneClick": "H3 Full Video One Click",
    "H3FullVideoStatus": "H3 Full Video Status",
}
