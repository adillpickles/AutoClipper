"""A deterministic synthetic dataset (fake VODs, clips, chat, audio, video) for development.

``autoclipper synth`` calls `generate_dataset`; see docs/development/testing.md § Fixtures.
"""

from autoclipper.synthetic.dataset import (
    GeneratedDataset,
    OutputDirError,
    default_output_dir,
    generate_dataset,
)
from autoclipper.synthetic.media import VideoOptions

__all__ = [
    "GeneratedDataset",
    "OutputDirError",
    "VideoOptions",
    "default_output_dir",
    "generate_dataset",
]
