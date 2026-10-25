"""Explicit original-NetCDF replay checks, outside the ordinary processing path.

The native-grid reference below does not call the production extractor, warp,
VRT builder or mosaic selector. It reads original variables and places them by
integer offsets in bounded windows. It is an audit, not another output product.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from swotflow.verification.native import compare_arrays, windows
from swotflow.verification.pipeline import VerificationStore, artifact_current, file_hash, utc_now


def check_sources(store: VerificationStore, output: Path, mode: str = "full") -> dict:
    """Reconstruct native WSE/QA directly from the exact original source bytes."""
    import numpy as np
    from osgeo import gdal
    gdal.UseExceptions()
    proof = store.artifact(output)
    if not proof or not artifact_current(proof):
        raise ValueError("Local output has no current file-identity proof")
    original_paths = []
    if proof["stage"] == "extract":
        original_paths = [Path(s) for s in proof["inputs"]]
    elif proof["stage"] == "mosaic":
        for source in proof["inputs"]:
            extraction = store.artifact(Path(source))
            if not extraction or extraction["sha256"] != proof["inputs"][source]:
                raise ValueError("Extraction history does not match the mosaic input hash")
            original_paths.extend(Path(p) for p in extraction["inputs"])
    else:
        raise ValueError("Choose an extraction or local mosaic output")
    original_paths = sorted(set(original_paths), key=lambda p: p.name)
    from swotflow.verification.pipeline import product_name
    if set(proof["sources"]) != {product_name(p) for p in original_paths}:
        raise ValueError("Original source membership does not reconcile")
    for path in original_paths:
        source = proof["sources"][product_name(path)]
        if not path.is_file():
            raise ValueError(f"Original source is absent; reacquire this exact version first: {path.name}")
        if file_hash(path, source["algorithm"]) != source["hash"]:
            raise ValueError("Original bytes changed: " + path.name)
    target = gdal.Open(str(output))
    if target is None or target.RasterCount != 2:
        raise ValueError("Expected a two-band target")
    transform = target.GetGeoTransform()
    if (transform[1], transform[2], transform[4], transform[5]) != (100, 0, 0, -100):
        raise ValueError("Independent replay supports native unrotated 100 m grids")
    datasets = []
    for path in original_paths:
        wse = gdal.Open(f'NETCDF:"{path}":wse')
        qa = gdal.Open(f'NETCDF:"{path}":wse_qual')
        if wse is None or qa is None:
            raise ValueError("Missing original WSE/quality variable: " + path.name)
        t = wse.GetGeoTransform()
        if (qa.GetGeoTransform() != t or not target.GetSpatialRef().IsSame(wse.GetSpatialRef())
                or not target.GetSpatialRef().IsSame(qa.GetSpatialRef())
                or (wse.RasterXSize, wse.RasterYSize) != (qa.RasterXSize, qa.RasterYSize)
                or (t[1], t[2], t[4], t[5]) != (100, 0, 0, -100)):
            raise ValueError("Incompatible native source grid: " + path.name)
        dx, dy = (t[0]-transform[0])/100, (transform[3]-t[3])/100
        if abs(dx-round(dx)) > 1e-8 or abs(dy-round(dy)) > 1e-8:
            raise ValueError("Fractional-cell source offset")
        x, y = round(dx), round(dy)
        if x < 0 or y < 0 or x+wse.RasterXSize > target.RasterXSize or y+wse.RasterYSize > target.RasterYSize:
            raise ValueError("Local output does not contain the full source extent")
        if proof["stage"] == "extract" and (x or y or wse.RasterXSize != target.RasterXSize or wse.RasterYSize != target.RasterYSize):
            raise ValueError("Extraction extent differs from the original")
        datasets.append((wse, qa, x, y))
    totals = {"pixels": 0, "wse_mismatch": 0, "quality_mismatch": 0, "mask_mismatch": 0,
              "local_valid": 0, "remote_valid": 0, "max_abs_wse_m": 0.0, "overlap_cells": 0}
    results = []
    for x, y, w, h in windows(target.RasterXSize, target.RasterYSize, mode):
        values, quality = np.zeros((h,w), dtype=np.float32), np.zeros((h,w), dtype=np.float32)
        valid, quality_valid = np.zeros((h,w), bool), np.zeros((h,w), bool)
        hits = np.zeros((h,w), dtype=np.uint16)
        for wse, qa, sx, sy in datasets:
            left, top = max(x,sx), max(y,sy)
            right, bottom = min(x+w,sx+wse.RasterXSize), min(y+h,sy+wse.RasterYSize)
            if right <= left or bottom <= top:
                continue
            window = (left-sx, top-sy, right-left, bottom-top)
            a = wse.GetRasterBand(1).ReadAsArray(*window).astype(np.float32)
            q = qa.GetRasterBand(1).ReadAsArray(*window).astype(np.float32)
            am = wse.GetRasterBand(1).GetMaskBand().ReadAsArray(*window) != 0
            qm = qa.GetRasterBand(1).GetMaskBand().ReadAsArray(*window) != 0
            sl = np.s_[top-y:bottom-y, left-x:right-x]
            if proof["stage"] == "extract":
                values[sl], quality[sl], valid[sl], quality_valid[sl] = a, q, am, qm
            else:
                eligible = am & qm & np.isfinite(a) & np.isin(q, [0,1,2,3])
                hits[sl] += eligible
                take = eligible & (~valid[sl] | (q < quality[sl]))
                values[sl][take], quality[sl][take] = a[take], q[take]
                valid[sl][take], quality_valid[sl][take] = True, True
        expected = {"b1": values, "b2": quality, "m1": valid, "m2": quality_valid}
        actual = {}
        for band in (1,2):
            b = target.GetRasterBand(band)
            actual[f"b{band}"] = b.ReadAsArray(x,y,w,h)
            actual[f"m{band}"] = b.GetMaskBand().ReadAsArray(x,y,w,h) != 0
        result = compare_arrays(expected, actual)
        result["overlap_cells"] = int(np.count_nonzero(hits > 1))
        results.append({"window": [x,y,w,h], **result})
        for key in totals:
            totals[key] = max(totals[key],result[key]) if key == "max_abs_wse_m" else totals[key]+result[key]
    if not artifact_current(proof):
        raise ValueError("Output changed during source replay")
    for path in original_paths:
        source = proof["sources"][product_name(path)]
        if file_hash(path, source["algorithm"]) != source["hash"]:
            raise ValueError("Original changed during source replay")
    result = {"utc": utc_now(), "mode": mode, "output": str(output), "output_sha256": proof["sha256"],
              "sources": proof["sources"], "totals": totals, "windows": results,
              "passed": not any(totals[k] for k in ("wse_mismatch", "quality_mismatch", "mask_mismatch")),
              "reference": "Original NetCDF variables; independent integer-grid placement and paired selection; same GDAL reader"}
    folder = store.logs / "source_pixel_checks"
    folder.mkdir(parents=True, exist_ok=True)
    from uuid import uuid4
    (folder / (uuid4().hex + ".json")).write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--logs", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mode", choices=("sampled", "full"), default="full")
    args = parser.parse_args()
    from swotflow.core.gdal import configure_current_process_gdal_env
    configure_current_process_gdal_env()
    result = check_sources(VerificationStore(args.logs), args.output, args.mode)
    print(json.dumps({k:v for k,v in result.items() if k != "windows"}), flush=True)
    raise SystemExit(0 if result["passed"] else 2)
