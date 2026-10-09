"""Synthetic datasets for tests and benchmarks, and mocks rebuilt from fingerprints."""

from dbx.synth.anndata_gen import write_anndata
from dbx.synth.matrix import SynthMatrix
from dbx.synth.mock import structure_signature, write_mock
from dbx.synth.spatial_gen import write_spatialdata

__all__ = ["SynthMatrix", "structure_signature", "write_anndata", "write_mock", "write_spatialdata"]
