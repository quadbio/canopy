"""Synthetic datasets for tests and benchmarks, and mocks rebuilt from fingerprints."""

from canopy.synth.anndata_gen import write_anndata
from canopy.synth.demo import write_demo
from canopy.synth.matrix import SynthMatrix
from canopy.synth.mock import structure_signature, write_mock
from canopy.synth.spatial_gen import write_spatialdata

__all__ = [
    "SynthMatrix",
    "structure_signature",
    "write_anndata",
    "write_demo",
    "write_mock",
    "write_spatialdata",
]
