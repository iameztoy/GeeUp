"""Ordinary image ingestion: retain the TIFF grid, mask, bands and metadata."""
from datetime import datetime, timezone
from swotflow.earthengine.uploader import compact_upload_properties
from swotflow.verification.pipeline import ingestion_properties


def timestamp(text):
    value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def build_manifest(config, item, parsed, row):
    if parsed.status != "METADATA_PARSED":
        raise ValueError("Cloud upload requires parsed SWOT acquisition metadata.")
    policy = config.upload.pyramiding_policy
    allowed = {"MEAN", "SAMPLE", "MODE", "MIN", "MAX"}
    default = (policy.default or "MEAN").upper()
    if default not in allowed or set(policy.per_band) - {"b1", "b2"}:
        raise ValueError("Unsupported pyramiding policy or band name; expected b1 (WSE) and b2 (quality).")
    bands = []
    for i, band in enumerate(("b1", "b2")):
        selected = str(policy.per_band.get(band, default)).upper()
        if selected not in allowed:
            raise ValueError("Unsupported band pyramiding policy: " + selected)
        bands.append({"id": band, "tilesetId": "raster", "tilesetBandIndex": i, "pyramidingPolicy": selected})
    manifest = {"name": item.asset_id, "tilesets": [{"id": "raster", "sources": [
        {"uris": [f"gs://{row['bucket']}/{row['object']}"]}]}], "bands": bands,
        "properties": {**compact_upload_properties(parsed), **ingestion_properties(row["receipt"])}}
    if config.metadata.add_start_time and parsed.start_time:
        manifest["startTime"] = timestamp(parsed.start_time)
    if config.metadata.add_end_time and parsed.end_time:
        manifest["endTime"] = timestamp(parsed.end_time)
    # No CRS/transform override, resampling, missingData override, or external_image.
    # Ordinary ingestion reads embedded GeoTIFF georeferencing and masks, like browser upload.
    return manifest
