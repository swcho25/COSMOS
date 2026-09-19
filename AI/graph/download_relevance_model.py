"""Download the official local relevance judge, pinned to an immutable revision.

Weights and the download receipt live in ignored local_models/. Safetensors only;
no repository Python is downloaded or executed. This is model acquisition, not
training, and it never uploads news. The receipt includes SHA256 checks against
the model publisher's LFS metadata where available.
"""
import argparse
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path


def ranged_download(repo, revision, item, destination, workers):
    """Bounded HTTPS ranges avoid this network's stalls on multi-GB responses."""
    import httpx
    target = destination / item.rfilename
    if target.exists() and target.stat().st_size == item.size:
        return
    chunk_size = 16 * 1024 * 1024
    chunks = destination / ".range_chunks" / item.rfilename
    chunks.mkdir(parents=True, exist_ok=True)

    def fetch(start):
        end = min(start + chunk_size, item.size) - 1
        path = chunks / f"{start:012d}.part"
        if path.exists() and path.stat().st_size == end - start + 1:
            return path
        url = f"https://huggingface.co/{repo}/resolve/{revision}/{item.rfilename}?download=true&range_start={start}"
        for attempt in range(4):
            try:
                with httpx.Client(follow_redirects=True, timeout=60) as client:
                    response = client.get(url, headers={"Range": f"bytes={start}-{end}"})
                    response.raise_for_status()
                    expected = f"bytes {start}-{end}/{item.size}"
                    if response.status_code != 206 or response.headers.get("content-range") != expected:
                        raise RuntimeError(f"Unexpected range response for {start}")
                    if len(response.content) != end - start + 1:
                        raise RuntimeError("Incomplete range")
                    path.write_bytes(response.content)
                    return path
            except (httpx.HTTPError, RuntimeError):
                if attempt == 3:
                    raise

    offsets = list(range(0, item.size, chunk_size))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(fetch, offset) for offset in offsets]
        for count, future in enumerate(as_completed(futures), 1):
            future.result()
            if count % 16 == 0 or count == len(offsets):
                print(json.dumps({"file": item.rfilename, "chunks_complete": count,
                                  "chunks_total": len(offsets)}), flush=True)
    partial = target.with_suffix(target.suffix + ".assembling")
    with partial.open("wb") as output:
        for offset in offsets:
            output.write((chunks / f"{offset:012d}.part").read_bytes())
    with partial.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != item.lfs.sha256:
        raise RuntimeError("Assembled model publisher SHA256 mismatch")
    partial.replace(target)
    for offset in offsets:
        (chunks / f"{offset:012d}.part").unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", default="main")
    parser.add_argument("--range-transfer", action="store_true")
    parser.add_argument("--download-workers", type=int, default=8, choices=range(1, 17))
    args = parser.parse_args()
    root = Path(__file__).resolve().parent / "local_models"
    root.mkdir(exist_ok=True)
    os.environ["HF_HOME"] = str(root / "hf_cache")
    os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
    # Plain HTTPS is more reliable than the optional Xet client on this PC.
    os.environ["HF_HUB_DISABLE_XET"] = "1"
    os.environ["HF_HUB_DOWNLOAD_TIMEOUT"] = "60"
    from huggingface_hub import HfApi, snapshot_download
    repo = "Qwen/Qwen3-4B-Instruct-2507"
    info = HfApi(token=False).model_info(repo, revision=args.revision, files_metadata=True)
    destination = root / "Qwen3-4B-Instruct-2507" / info.sha
    print(json.dumps({"repo": repo, "revision": info.sha, "destination": str(destination)}), flush=True)
    if args.range_transfer:
        for item in info.siblings:
            if item.rfilename.endswith(".safetensors"):
                ranged_download(repo, info.sha, item, destination, args.download_workers)
    snapshot_download(repo, revision=info.sha, token=False, local_dir=destination,
                      allow_patterns=["*.json", "*.safetensors", "*.txt", "*.jinja", "LICENSE", "README.md"],
                      ignore_patterns=["*.safetensors"] if args.range_transfer else None,
                      max_workers=3)
    metadata = {item.rfilename: item for item in info.siblings}
    files = []
    for path in sorted(destination.rglob("*")):
        if not path.is_file() or any(part.startswith(".") for part in path.relative_to(destination).parts):
            continue
        name = path.relative_to(destination).as_posix()
        digest = hashlib.file_digest(path.open("rb"), "sha256").hexdigest()
        item = metadata.get(name)
        expected = item.lfs.sha256 if item and item.lfs else None
        if expected and digest != expected:
            raise RuntimeError(f"Publisher SHA256 mismatch: {name}")
        files.append({"path": name, "bytes": path.stat().st_size, "sha256": digest,
                      "publisher_sha256": expected})
    receipt = {"repo": repo, "revision": info.sha, "directory": str(destination),
               "downloaded_at": datetime.now(timezone.utc).isoformat(), "files": files,
               "total_bytes": sum(item["bytes"] for item in files), "trust_remote_code": False}
    (root / "relevance_model_receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    print(json.dumps({"verified_files": len(files), "total_bytes": receipt["total_bytes"]}), flush=True)


if __name__ == "__main__":
    main()
