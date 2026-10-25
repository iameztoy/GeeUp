"""On-demand, bounded-memory comparison of local native rasters with EE pixels."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

from pipeline_verification import VerificationStore, artifact_current, digest, utc_now

BLOCK_SIZE = 512


def windows(width: int, height: int, mode: str) -> list[tuple[int, int, int, int]]:
    """Cover all cells, or a deterministic spatial sample explicitly labelled partial."""
    if width <= 0 or height <= 0 or mode not in {"sampled", "full"}:
        raise ValueError("Invalid dimensions or comparison mode")
    if mode == "full":
        return [(x, y, min(BLOCK_SIZE, width-x), min(BLOCK_SIZE, height-y))
                for y in range(0, height, BLOCK_SIZE) for x in range(0, width, BLOCK_SIZE)]
    w, h = min(128, width), min(128, height)
    return sorted({(round((width-w)*i/4), round((height-h)*j/4), w, h) for i in range(5) for j in range(5)})


def compare_arrays(local: dict, remote: Any, tolerance: float = 1e-5) -> dict:
    """Compare each band's mask separately, WSE in metres and categorical QA exactly."""
    import numpy as np
    result = {"pixels": int(local["b1"].size), "wse_mismatch": 0, "quality_mismatch": 0,
              "mask_mismatch": 0, "local_valid": 0, "remote_valid": 0, "max_abs_wse_m": 0.0}
    for band in (1, 2):
        a, b = local[f"b{band}"], remote[f"b{band}"]
        am, bm = local[f"m{band}"].astype(bool), remote[f"m{band}"].astype(bool)
        if a.shape != b.shape or a.shape != am.shape or b.shape != bm.shape:
            raise ValueError("Pixel window shape mismatch")
        result["mask_mismatch"] += int(np.count_nonzero(am != bm))
        both = am & bm
        different = both & (~np.isfinite(a) | ~np.isfinite(b) | (np.abs(a.astype(float)-b.astype(float)) > (tolerance if band == 1 else 0)))
        result["wse_mismatch" if band == 1 else "quality_mismatch"] = int(np.count_nonzero(different))
        if band == 1:
            result["local_valid"] = int(am.sum())
            result["remote_valid"] = int(bm.sum())
            valid = both & np.isfinite(a) & np.isfinite(b)
            result["max_abs_wse_m"] = float(np.max(np.abs(a[valid].astype(float)-b[valid].astype(float)), initial=0))
    return result


def raster_reader(path: Path, scratch: Path) -> None:
    """Serve bounded GDAL windows over stdin; no EE or authentication in this child."""
    import numpy as np
    from osgeo import gdal
    gdal.UseExceptions()
    ds = gdal.Open(str(path))
    if ds is None or ds.RasterCount != 2:
        raise ValueError("Expected a two-band local WSE/QA mosaic")
    srs = ds.GetSpatialRef()
    if srs is None:
        raise ValueError("Raster CRS missing")
    srs.AutoIdentifyEPSG()
    crs = "EPSG:" + str(srs.GetAuthorityCode(None))
    grid = {"width": ds.RasterXSize, "height": ds.RasterYSize, "crs": crs, "transform": list(ds.GetGeoTransform())}
    print(json.dumps(grid), flush=True)
    for line in sys.stdin:
        request = json.loads(line)
        data = {}
        for b in (1, 2):
            band = ds.GetRasterBand(b)
            data[f"b{b}"] = band.ReadAsArray(*request)
            data[f"m{b}"] = band.GetMaskBand().ReadAsArray(*request) != 0
        target = scratch / "window.npz"
        np.savez(target, **data)
        print(json.dumps({"path": str(target)}), flush=True)


def check_native_pixels(store: VerificationStore, local_path: Path, asset_id: str, gdal_python: Path,
                        mode: str = "sampled", progress: Callable[[str], None] | None = None,
                        stop: Any = None) -> dict:
    """Compare one current local mosaic with one unchanged EE asset; never writes EE."""
    import ee
    import numpy as np
    from ee_consolidation import snapshot_asset
    from gdal_runtime import build_gdal_runtime_env
    proof = store.artifact(local_path)
    if not proof or proof["stage"] != "mosaic" or not artifact_current(proof):
        raise ValueError("A current local mosaic proof is required; rebuild unverified/stale files first")
    if not asset_id.startswith("projects/") or "/assets/" not in asset_id:
        raise ValueError("Use a full Earth Engine image asset ID")
    ee.Initialize(project=asset_id.split("/")[1])
    before = snapshot_asset(ee.data.getAsset(asset_id))
    receipt = {"utc": utc_now(), "asset_id": asset_id, "asset_fingerprint": digest(before),
               "local_sha256": proof["sha256"], "sources": proof["sources"], "mode": mode,
               "local_auxiliary_hashes": proof.get("auxiliary_hashes", {}),
               "passed": False, "wse_tolerance_m": 1e-5, "windows": [], "api_version": ee.__version__,
               "reference": "local mosaic (not an independent replay of the original NetCDF)"}
    child = None
    try:
        with tempfile.TemporaryDirectory(prefix="swotflow-pixel-check-") as temp:
            child = subprocess.Popen([str(gdal_python), str(Path(__file__).resolve()), "--read-raster", str(local_path), "--scratch", temp],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     text=True, encoding="utf-8", env=build_gdal_runtime_env(gdal_python),
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            grid = json.loads(child.stdout.readline())
            t = grid["transform"]
            if t[1] != 100 or t[5] != -100 or t[2] != 0 or t[4] != 0 or not grid["crs"].startswith(("EPSG:326", "EPSG:327")):
                raise ValueError("This check requires an unrotated native 100 m UTM grid")
            bands = {b["id"]: b for b in before.get("bands", [])}
            if not {"b1", "b2"} <= bands.keys():
                raise ValueError("EE asset lacks b1/b2")
            expected = {"scaleX": 100, "shearX": 0, "translateX": t[0], "shearY": 0, "scaleY": -100, "translateY": t[3]}
            for name in ("b1", "b2"):
                native = bands[name]["grid"]
                actual = {k: native["affineTransform"].get(k, 0) for k in expected}
                if (native["crsCode"] != grid["crs"] or actual != expected or
                    native["dimensions"] != {"width": grid["width"], "height": grid["height"]}):
                    raise ValueError("EE/local native grid or extent mismatch; refusing resampled verification")
            image = ee.Image(asset_id).select(["b1", "b2"])
            expression = image.unmask(0, False).addBands(image.mask().rename(["m1", "m2"]).unmask(0, False))
            planned = windows(grid["width"], grid["height"], mode)
            totals = {"pixels": 0, "wse_mismatch": 0, "quality_mismatch": 0, "mask_mismatch": 0,
                      "local_valid": 0, "remote_valid": 0, "max_abs_wse_m": 0.0}
            for index, window in enumerate(planned, 1):
                if stop is not None and stop.is_set():
                    raise InterruptedError("Comparison stopped; incomplete results cannot certify pixels")
                x, y, w, h = window
                child.stdin.write(json.dumps(window) + "\n")
                child.stdin.flush()
                info = json.loads(child.stdout.readline())
                affine = {**expected, "translateX": t[0]+x*100, "translateY": t[3]-y*100}
                remote = ee.data.computePixels({"expression": expression, "fileFormat": "NUMPY_NDARRAY",
                    "grid": {"dimensions": {"width": w, "height": h}, "affineTransform": affine, "crsCode": grid["crs"]}})
                with np.load(info["path"]) as local:
                    result = compare_arrays(local, remote)
                receipt["windows"].append({"window": window, **result})
                for key in totals:
                    totals[key] = max(totals[key], result[key]) if key == "max_abs_wse_m" else totals[key]+result[key]
                if progress:
                    progress(f"{local_path.name}: {index}/{len(planned)} windows, {totals['wse_mismatch']} WSE differences")
            if digest(snapshot_asset(ee.data.getAsset(asset_id))) != receipt["asset_fingerprint"] or not artifact_current(proof):
                raise ValueError("Input changed during comparison")
            receipt.update(totals=totals, grid=grid, passed=not any(totals[k] for k in ("wse_mismatch", "quality_mismatch", "mask_mismatch")))
    except Exception as exc:
        receipt["error"] = type(exc).__name__ + ": " + str(exc)[:600]
        raise
    finally:
        if child:
            if child.stdin:
                child.stdin.close()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        receipt["finished_utc"] = utc_now()
        store.record_cloud(receipt)
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--read-raster", type=Path, required=True)
    parser.add_argument("--scratch", type=Path, required=True)
    args = parser.parse_args()
    raster_reader(args.read_raster, args.scratch)
