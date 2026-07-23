"""Fingerprint packages — a portable head-slice of a dataset that reproduces its full identity.

``preflight`` cuts every FASTQ down to its first N records, extracts any paper/spreadsheet prose, and
writes a pin (``fingerprint.json``) that carries the whole-file identity the slice cannot recompute.
The result is a ``<dataset>.fingerprint.tar.gz`` small enough to carry off the machine, from which the
whole pipeline (probe → resolve → harvest → manifest → compose) reproduces the same ``manifest.yaml``
— including a byte-identical ``dataset_hash`` — even after the original FASTQs are gone.

Two halves, cleanly split:

- :mod:`.subsample` cuts the reads (bounded, never a whole-file read) and re-emits reproducible gzip.
- :mod:`.build` orchestrates a full-file probe for the pin, the slicing, the info extraction, and the
  deterministic packaging; :mod:`.load` reconstructs the stand-in probe map from a package so a
  fingerprint run resolves exactly as the full FASTQs would.
"""

from __future__ import annotations

from ..models.fingerprint import FINGERPRINT_VERSION

__all__ = ["FINGERPRINT_VERSION"]
