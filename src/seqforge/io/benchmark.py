"""Pull a fingerprint package from the public HF benchmark dataset — pooch-cached, no SDK, no token.

seqforge's eval corpus splits in two by where its inputs live. The **ci-benchmark** is committed to
git (synthetic recipes and, later, tiny real fingerprints) and runs offline on every commit, so a free
HF account's rate limits can never break normal CI. The growing **validation benchmark** — real
datasets too large or too numerous to commit — lives on the public HF dataset repo
``liuhlab/seqforge-benchmark`` and is pulled only by the opt-in / scheduled eval job.

The pull needs neither ``huggingface_hub`` nor a token. A *public* HF dataset serves every file at a
stable URL — ``https://huggingface.co/datasets/<repo>/resolve/<rev>/<path>`` — over ordinary HTTPS, and
anonymous read is a plain GET. So a package is fetched with exactly the pooch call the onlist registry
already uses (:func:`seqforge.io.onlist.OnlistRegistry._fetch`), cached under the OS cache dir, with no
new dependency. Only *uploading* a package needs the maintainer's HF write token — a producer concern,
out of this consumer's scope.
"""

from __future__ import annotations

from pathlib import Path

#: The public HF dataset repo the validation benchmark is published to. Public => anonymous read =>
#: the networked eval job needs no CI secret; only the maintainer's upload uses a write token.
HF_BENCHMARK_REPO = "liuhlab/seqforge-benchmark"


class BenchmarkPackageUnavailable(RuntimeError):
    """A benchmark package could not be fetched (offline, missing, or the repo is unreachable).

    Raised rather than returned so a caller can map it onto the eval harness's *skip* — a package that
    is not reachable here is exactly a case that must not run, never one that fails.
    """


def hf_package_url(rel_path: str, *, repo: str = HF_BENCHMARK_REPO, revision: str = "main") -> str:
    """The stable public URL a fingerprint package resolves to on the HF dataset repo.

    ``rel_path`` is the package's path within the repo (e.g. ``packages/GSE274290.fingerprint.tar.gz``).
    No token, no API — the ``resolve`` endpoint streams the raw bytes to an anonymous GET.
    """
    return f"https://huggingface.co/datasets/{repo}/resolve/{revision}/{rel_path.lstrip('/')}"


def fetch_benchmark_package(
    rel_path: str,
    *,
    repo: str = HF_BENCHMARK_REPO,
    revision: str = "main",
    cache_dir: str | Path | None = None,
) -> Path:
    """Fetch a fingerprint package from the public HF benchmark and return its cached local path.

    Pooch caches by URL, so a second request for the same package hits disk, not the network — the
    same contract the onlist fetch relies on. ``known_hash=None`` for the same reason it is ``None``
    there: the package is content-addressed by its own pin and re-verified downstream (a fingerprint
    run reproduces the dataset hash), so pinning the *download* would only break on a re-compression
    while proving nothing about the reads. Any network/resolution failure becomes
    :class:`BenchmarkPackageUnavailable`, which the eval harness turns into a skip.
    """
    import pooch  # local import: keep the module importable offline / without pooch resolved

    url = hf_package_url(rel_path, repo=repo, revision=revision)
    try:
        return Path(
            pooch.retrieve(
                url=url,
                known_hash=None,
                path=str(cache_dir) if cache_dir is not None else None,
                fname=Path(rel_path).name,
                progressbar=False,
            )
        )
    except Exception as exc:  # noqa: BLE001 - any fetch failure is a skip, not a crash
        raise BenchmarkPackageUnavailable(
            f"could not fetch benchmark package {rel_path!r} from {url}: {exc}"
        ) from exc


__all__ = [
    "HF_BENCHMARK_REPO",
    "BenchmarkPackageUnavailable",
    "fetch_benchmark_package",
    "hf_package_url",
]
