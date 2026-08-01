"""The two ends of the benchmark corpus: pull a fingerprint package, and publish one.

seqforge's eval corpus splits in two by where its inputs live. The **ci-benchmark** is committed to
git (synthetic recipes and, later, tiny real fingerprints) and runs offline on every commit, so a free
HF account's rate limits can never break normal CI. The growing **validation benchmark** — real
datasets too large or too numerous to commit — lives on the public HF dataset repo
``liuhlab/seqforge-benchmark`` and is pulled only by the opt-in / scheduled eval job.

**Reading needs neither ``huggingface_hub`` nor a token; writing needs both.** A *public* HF dataset
serves every file at a stable URL — ``https://huggingface.co/datasets/<repo>/resolve/<rev>/<path>`` —
over ordinary HTTPS, and anonymous read is a plain GET. So a package is fetched with exactly the pooch
call the onlist registry already uses (:func:`seqforge.io.onlist.OnlistRegistry._fetch`), cached under
the OS cache dir. Publishing is a commit to the repo, which is an authenticated API call, so
:func:`publish_benchmark_package` uses ``HfApi().upload_file`` and the maintainer's write token. The
asymmetry is the point: the consumer stays dependency-free and credential-free, and the producer half
is a verb rather than a memory.

**A missing package is not a network failure.** The archive answering *404* means it was reached and
does not hold this package — the dataset was never published, which is a gap in the corpus and a fact
the eval report says out loud. Everything else the wire can do (offline, DNS, a rate limit, a 5xx)
says nothing at all about whether the package exists. Hence two exception types, one a subclass of the
other so every caller that only wants "it did not arrive" is unchanged.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from ..models.fingerprint import PublishedPackage

#: The public HF dataset repo the validation benchmark is published to. Public => anonymous read =>
#: the networked eval job needs no CI secret; only the maintainer's upload uses a write token.
HF_BENCHMARK_REPO = "liuhlab/seqforge-benchmark"

#: Where a package belongs inside that repo. One directory, so a package's path is derivable from its
#: filename and nobody has to remember a layout.
HF_PACKAGE_PREFIX = "packages"


class BenchmarkPackageUnavailable(RuntimeError):
    """A benchmark package did not arrive: offline, DNS, a rate limit, or the archive is unwell.

    Raised rather than returned so a caller can map it onto the eval harness's *skip* — nothing was
    learned here about whether the package exists, so a case that wanted it must not run, and must
    not fail either. See :class:`BenchmarkPackageAbsent` for the case where something *was* learned.
    """


class BenchmarkPackageAbsent(BenchmarkPackageUnavailable):
    """The archive answered, and it does not hold this package — it was never published.

    A **gap in the corpus**, not bad weather. The eval harness still declines to fail the run (the
    benchmark tier is opt-in and never gates a merge), but it reports the state under its own name so
    a dataset silently missing from the corpus cannot read as a transient network skip. Subclasses
    :class:`BenchmarkPackageUnavailable` so an ``except`` that only cares about arrival is unchanged.
    """


def hf_package_url(rel_path: str, *, repo: str = HF_BENCHMARK_REPO, revision: str = "main") -> str:
    """The stable public URL a fingerprint package resolves to on the HF dataset repo.

    ``rel_path`` is the package's path within the repo (e.g. ``packages/GSE274290.fingerprint.tar.gz``).
    No token, no API — the ``resolve`` endpoint streams the raw bytes to an anonymous GET.
    """
    return f"https://huggingface.co/datasets/{repo}/resolve/{revision}/{rel_path.lstrip('/')}"


def _http_status(exc: BaseException) -> int | None:
    """The HTTP status behind a failed fetch, or ``None`` if no response ever came back.

    Read off the exception **chain**, not off the top-level exception: pooch downloads through a
    pluggable downloader, and one that re-raised its own type ``from`` the ``requests`` error would
    otherwise demote a 404 to "offline" — which is exactly the conflation this module exists to end.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        status = getattr(getattr(current, "response", None), "status_code", None)
        if isinstance(status, int):
            return status
        current = current.__cause__ or current.__context__
    return None


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
    while proving nothing about the reads.

    A **404** raises :class:`BenchmarkPackageAbsent` — the repo was reached and has no such package.
    Anything else raises :class:`BenchmarkPackageUnavailable`. The eval harness turns both into a
    skip and reports them under different names.
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
    except Exception as exc:  # noqa: BLE001 - a fetch failure is a skip, not a crash
        if _http_status(exc) == 404:
            raise BenchmarkPackageAbsent(
                f"benchmark package {rel_path!r} was never published to {repo}: "
                f"{url} answers 404. Publish it with `seqforge io publish-package`."
            ) from exc
        raise BenchmarkPackageUnavailable(
            f"could not fetch benchmark package {rel_path!r} from {url}: {exc}"
        ) from exc


#: The content-address ``workspace.readable`` appends to a freshly built package's stem. Stripped
#: below, and matched rather than assumed so a package a human already named is left alone.
_BUILD_DIGEST = re.compile(r"-[0-9a-f]{8,}(?=\.fingerprint\.tar\.gz$)")


def default_package_path(package: str | Path, *, prefix: str = HF_PACKAGE_PREFIX) -> str:
    """Where a local package belongs in the repo: ``packages/<dataset>.fingerprint.tar.gz``.

    Derived rather than typed, because the fetch side derives it too — an eval recipe's ``hf:`` key
    is written by hand and a package uploaded under a name that does not match it is a 404 nobody
    sees until the benchmark runs.

    **The build's content-address is dropped**, and that is the whole subtlety here. ``preflight``
    names its output ``GSE110823-b07aee1dd1eb.fingerprint.tar.gz``: the digest keeps two builds of
    one dataset apart on disk, which is exactly right for a workspace and exactly wrong for a corpus
    key, because a recipe pinned to it would 404 the moment the package was rebuilt. In the corpus a
    package is identified by its dataset, and its content is re-verified by the run that reads it.
    """
    return f"{prefix.strip('/')}/{_BUILD_DIGEST.sub('', Path(package).name)}"


def publish_benchmark_package(
    package: str | Path,
    *,
    rel_path: str | None = None,
    repo: str = HF_BENCHMARK_REPO,
    revision: str = "main",
    token: str | None = None,
    message: str | None = None,
    dry_run: bool = False,
    api: Any = None,
) -> PublishedPackage:
    """Publish one fingerprint package to the benchmark repo — the producer half of the contract.

    ``preflight`` builds a package; this puts it in the corpus, so a dataset's route from a maintainer's
    disk to :func:`fetch_benchmark_package` is a verb with an exit code rather than a remembered
    sequence of clicks. The package is **read and validated before anything is uploaded**: it must be
    a real fingerprint package (its pin parses) or the upload is refused, because a corrupt tarball on
    the public repo turns every future run of that case into a mystery rather than a skip.

    ``dry_run`` resolves the destination, hashes the bytes and reports what *would* be uploaded,
    touching no credential and no network — so "where does this go and under what name" is answerable
    without spending a commit. ``api`` is an injection seam for tests; production leaves it ``None``
    and this constructs ``HfApi``. The upload goes through ``HfApi().upload_file`` and never the ``hf``
    command-line client, which hangs.
    """
    from ..fingerprint.load import read_pin

    src = Path(package)
    if not src.is_file():
        raise FileNotFoundError(f"no fingerprint package at {src}")
    pin = read_pin(src)  # refuses anything that is not a package, before a byte is sent

    target = (rel_path or default_package_path(src)).lstrip("/")
    with src.open("rb") as handle:  # streamed: a package is carried, not held
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    result = PublishedPackage(
        package=str(src),
        repo=repo,
        revision=revision,
        rel_path=target,
        url=hf_package_url(target, repo=repo, revision=revision),
        size_bytes=src.stat().st_size,
        sha256=digest,
        n_files=len(pin.files),
        reads=pin.reads,
        dry_run=dry_run,
    )
    if dry_run:
        return result

    if api is None:
        from huggingface_hub import HfApi  # local: nothing on the reading side imports this

        api = HfApi(token=token)
    info = api.upload_file(
        path_or_fileobj=str(src),
        path_in_repo=target,
        repo_id=repo,
        repo_type="dataset",
        revision=revision,
        commit_message=message or f"Publish {target}",
    )
    commit = getattr(info, "commit_url", None)
    return result.model_copy(update={"commit_url": str(commit) if commit else None})


__all__ = [
    "HF_BENCHMARK_REPO",
    "HF_PACKAGE_PREFIX",
    "BenchmarkPackageAbsent",
    "BenchmarkPackageUnavailable",
    "default_package_path",
    "fetch_benchmark_package",
    "hf_package_url",
    "publish_benchmark_package",
]
