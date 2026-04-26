# SOP — SageMaker Geospatial ML (Earth observation imagery · built-in models · raster ops · vector overlays)

**Version:** 2.0 · **Last-reviewed:** 2026-04-26 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · SageMaker Geospatial capabilities (GA 2023, ongoing 2024-2026 enhancements) · Earth Observation jobs · pre-trained geospatial models (cloud removal, semantic segmentation, change detection) · raster + vector data ops · Sentinel-2 + Landsat + commercial imagery providers · Geospatial Studio app

---

## 1. Purpose

- Codify the **Geospatial ML pattern** for engagements involving satellite imagery, aerial photos, GeoTIFF rasters, or vector geo data.
- Cover **Earth Observation Jobs** — the managed pipeline for pulling Sentinel-2 / Landsat scenes from AWS Open Data, applying transformations, and running pre-built models.
- Cover the **8+ pre-built models**: cloud removal, cloud masking, land use / land cover, water-body detection, building footprint extraction, vegetation health, change detection, semantic segmentation.
- Codify the **integration with custom training** — when pre-built doesn't fit, train your own on geospatial features.
- This is the **geospatial-vertical specialisation**. Niche but high-value for agriculture, insurance, forestry, urban planning, disaster response engagements.

When the SOW signals: "satellite imagery", "geospatial analytics", "land use classification", "crop health monitoring", "deforestation tracking", "post-disaster damage assessment", "GeoTIFF processing".

---

## 2. Decision tree — Geospatial flavors

```
What kind of imagery?
├── Sentinel-2 / Landsat (free, public) → §3 Earth Observation Job (managed pull from open data)
├── Commercial satellite (Planet, Maxar) → bring-your-own; upload to S3
├── Drone / aerial → bring-your-own; ortho-corrected GeoTIFF
└── LiDAR / point cloud → not yet supported in SageMaker; use EMR Serverless + custom

Use case?
├── Cloud removal / cloud masking → §3.4 pre-built model
├── Land use classification → §3.4 pre-built (LULC)
├── Change detection (before/after) → §3.4 pre-built
├── Object detection (boats, cars) → §4 custom training
├── Vegetation NDVI calculation → §3.5 raster ops (no ML needed, just math)
└── Custom segmentation → §4 custom training w/ pre-built backbone

Output format?
├── Raster (GeoTIFF) → S3 output
├── Vector (GeoJSON) → output_config.vector
├── Tabular (CSV w/ lat/lon) → output_config.attributes
└── Visualization (web tile) → SageMaker Geospatial Studio app
```

---

## 3. Earth Observation Job — `_create_eo_job_pipeline()`

### 3.1 Architecture

```
   ┌──────────────────────────────────────────────────────────────────┐
   │  AWS Open Data Registry: Sentinel-2 (s3://sentinel-2-l2a-cogs/)  │
   │     - Free                                                          │
   │     - Multi-band imagery, 10m resolution, 5-day revisit              │
   └──────────────────┬───────────────────────────────────────────────┘
                      │
                      │  Earth Observation Job pulls scenes by AOI + date range
                      ▼
   ┌──────────────────────────────────────────────────────────────────┐
   │  EOJ: my-cloud-removal-job                                        │
   │     - Input: AOI polygon + date range                              │
   │     - Filters: cloud cover < 20%                                    │
   │     - Pre-built model: CloudRemoval                                 │
   │     - Output: cleaned multi-band GeoTIFF per scene                  │
   └──────────────────┬───────────────────────────────────────────────┘
                      │
                      ▼
   S3 output: s3://qra-eoj-output/cloud-removal/<job-id>/<scene>.tif
```

### 3.2 CDK trigger Lambda

```python
"""eoj_trigger/index.py — kicks off an Earth Observation Job."""
import boto3, os
sg = boto3.client("sagemaker-geospatial")


def handler(event, context):
    """event: { 'aoi': [[lat,lon], ...], 'date_range': ('2026-04-01', '2026-04-15') }"""

    aoi_polygon = {
        "Polygon": {
            "Coordinates": [event["aoi"]],
        },
    }

    response = sg.start_earth_observation_job(
        Name=f"qra-eoj-{event['aoi_name']}-{int(time.time())}",
        InputConfig={
            "RasterDataCollectionQuery": {
                "RasterDataCollectionArn": "arn:aws:sagemaker-geospatial:us-west-2:378778860802:raster-data-collection/public/nmqj48dcu3g7ayw8",
                # Sentinel-2 L2A on AWS — public collection ARN
                "TimeRangeFilter": {
                    "StartTime": event["date_range"][0],
                    "EndTime":   event["date_range"][1],
                },
                "AreaOfInterest": {
                    "AreaOfInterestGeometry": aoi_polygon,
                },
                "PropertyFilters": {
                    "Properties": [
                        {"Property": {"EoCloudCover": {"LowerBound": 0, "UpperBound": 20}}},
                    ],
                    "LogicalOperator": "AND",
                },
            },
        },
        JobConfig={
            # Pre-built models: pick one
            "CloudRemovalConfig": {
                "AlgorithmName":             "INTERPOLATION",
                "InterpolationValue":        "-9999",
                "TargetBands":               ["red", "green", "blue", "nir"],
            },
            # Or land-use:
            # "LandCoverSegmentationConfig": {},
            # Or stack-and-export:
            # "StackConfig": {"OutputResolution": {"Predefined": "MEDIUM"}, "TargetBands": ["red","green","blue"]},
            # Or NDVI calc:
            # "BandMathConfig": {
            #     "PredefinedIndices": ["NDVI"],
            # },
        },
        OutputConfig={
            "S3Data": {
                "S3Uri":     os.environ["OUTPUT_S3_URI"],
                "KmsKeyId":  os.environ["KMS_KEY_ARN"],
            },
        },
        ExecutionRoleArn=os.environ["EOJ_ROLE_ARN"],
    )
    return {"jobArn": response["Arn"]}
```

### 3.3 CDK setup

```python
def _create_geospatial_resources(self, stage: str) -> None:
    """Output bucket + IAM + trigger Lambda."""

    self.eoj_output = s3.Bucket(self, "EoJobOutput",
        bucket_name=f"{{project_name}}-eoj-{stage}",
        encryption=s3.BucketEncryption.KMS,
        encryption_key=self.kms_key,
        block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
        enforce_ssl=True,
    )

    self.eoj_role = iam.Role(self, "EoJobRole",
        assumed_by=iam.ServicePrincipal("sagemaker-geospatial.amazonaws.com"),
        permissions_boundary=self.permission_boundary,
    )
    self.eoj_output.grant_read_write(self.eoj_role)
    self.kms_key.grant_encrypt_decrypt(self.eoj_role)
    self.eoj_role.add_to_policy(iam.PolicyStatement(
        actions=["sagemaker-geospatial:*"],
        resources=["*"],
    ))

    trigger_fn = lambda_.Function(self, "EoJobTriggerFn",
        runtime=lambda_.Runtime.PYTHON_3_12,
        handler="index.handler",
        code=lambda_.Code.from_asset(str(LAMBDA_SRC / "eoj_trigger")),
        timeout=Duration.minutes(5),
        environment={
            "OUTPUT_S3_URI":  f"s3://{self.eoj_output.bucket_name}/",
            "EOJ_ROLE_ARN":   self.eoj_role.role_arn,
            "KMS_KEY_ARN":    self.kms_key.key_arn,
        },
    )
    trigger_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["sagemaker-geospatial:StartEarthObservationJob",
                 "sagemaker-geospatial:GetEarthObservationJob"],
        resources=["*"],
    ))
    trigger_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["iam:PassRole"],
        resources=[self.eoj_role.role_arn],
    ))
```

### 3.4 Pre-built models

| Model | Use case | Input bands | Output |
|---|---|---|---|
| **Cloud removal** | Replace cloud-occluded pixels | red, green, blue, nir | Cleaned GeoTIFF |
| **Cloud masking** | Identify cloud-covered pixels | all S2 bands | Binary mask |
| **Land use / Land cover** | Classify each pixel into class | all S2 bands | Classification raster |
| **NDVI** | Vegetation health index | red + nir | Single-band float raster |
| **EVI** | Enhanced vegetation index | red + nir + blue | Single-band float raster |
| **NDWI** | Water-body detection | green + nir | Binary mask |
| **Building footprint** | Detect built structures | all S2 bands + 10m aerial | GeoJSON polygons |
| **Change detection** | Before/after pixel-level diff | 2 input rasters | Diff raster |
| **Semantic segmentation (custom)** | Generic pixel-class via custom model | configurable | Class raster |

### 3.5 Band math (no ML needed)

For simple indices like NDVI = (NIR - Red) / (NIR + Red):

```python
job_config = {
    "BandMathConfig": {
        "CustomIndices": {
            "Operations": [
                {"Name": "NDVI", "Equation": "(nir - red) / (nir + red + 1e-9)", "OutputType": "FLOAT32"},
            ],
        },
    },
}
```

---

## 4. Custom training on geospatial data

When pre-built models don't fit — e.g. detecting specific crop diseases:

```python
# Standard SageMaker Training Job with geospatial container
estimator = sagemaker.PyTorch(
    entry_point="train_segmentation.py",
    source_dir="scripts/",
    role=role,
    instance_type="ml.g5.4xlarge",
    framework_version="2.4.0",
    py_version="py311",
    image_uri=(
        # SageMaker geospatial-pre-built container with rasterio, geopandas, torch-vision
        "081189585077.dkr.ecr.us-west-2.amazonaws.com/sagemaker-geospatial:1.0-cpu-py311"
    ),
    hyperparameters={
        "model_arch":   "deeplabv3plus",
        "num_classes":  5,                                    # bg, healthy, diseased, water, cloud
        "input_bands":  "red,green,blue,nir,swir",
        "tile_size":    256,
    },
)
estimator.fit({
    "train":  "s3://qra-eoj-output/train-tiles/",
    "val":    "s3://qra-eoj-output/val-tiles/",
})
```

`scripts/train_segmentation.py` uses `rasterio` + `geopandas` (preinstalled in container) to load GeoTIFF tiles + masks.

---

## 5. Common gotchas + decisions matrix

| Symptom | Root cause | Fix |
|---|---|---|
| Job fails with "no scenes found" | AOI too small or date range too narrow | Expand AOI 10× or extend date range |
| Output GeoTIFF has wrong CRS | Default reprojection | Set `OutputConfig.ReprojectionConfig.TargetCrs` explicitly |
| Cloud masking misses thin clouds | Algorithm conservative | Try `LandCoverSegmentationConfig` instead — more aggressive |
| Pre-built model accuracy poor on local imagery | Trained on global Sentinel-2; local conditions may differ | Fine-tune custom segmentation model w/ local labels |
| EOJ takes hours despite small AOI | Date range too long → many scenes | Limit date range; tighten cloud cover filter |
| Geospatial Studio app fails to load | Region not us-west-2 | Geospatial only in us-west-2 currently |
| Custom container fails | Missing rasterio / geopandas | Use the SageMaker Geospatial container as base |

### 5.1 Region availability + cost

- **Geospatial-only available in us-west-2** as of 2026-04 (single-region service)
- **EOJ pricing:** $0.40 per scene processed for pre-built models; band math is free
- **Custom training:** standard SageMaker pricing
- **Geospatial Studio:** $0.30/hr per active session

---

## 6. Five non-negotiables

1. **us-west-2 region pinning.** Don't try to run EOJ in another region. Cross-region copy outputs to your home region after.
2. **Output bucket KMS-encrypted with project key.** Imagery often contains commercially sensitive data (e.g. precise farm locations).
3. **AOI bounds reasonable.** A 100° × 100° AOI = thousands of Sentinel-2 scenes = $$$$. Start with 1° × 1° boxes, expand if needed.
4. **Cloud cover filter < 20% for usable imagery.** Without it, you process clouds — wastes EOJ compute.
5. **Output format = COG (Cloud Optimized GeoTIFF).** Default. Don't downgrade to plain GeoTIFF — COG enables remote tile serving for Studio app + dashboards.

---

## 7. References

- AWS docs:
  - [SageMaker Geospatial overview](https://docs.aws.amazon.com/sagemaker/latest/dg/geospatial.html)
  - [Earth Observation Jobs](https://docs.aws.amazon.com/sagemaker/latest/dg/geospatial-eoj.html)
  - [Pre-built models](https://docs.aws.amazon.com/sagemaker/latest/dg/geospatial-models.html)
  - [Open data registry — Sentinel-2](https://registry.opendata.aws/sentinel-2/)
- Related SOPs:
  - `MLOPS_SAGEMAKER_TRAINING` — custom training on geospatial data
  - `MLOPS_BATCH_TRANSFORM` — bulk inference on imagery archives
  - `LAYER_NETWORKING` — VPC config for cross-region copy

---

## 8. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-26 | Initial partial — Earth Observation Jobs (Sentinel-2 / Landsat from AWS Open Data) + pre-built models (cloud removal, LULC, NDVI, building footprint, change detection) + custom training pattern. CDK + trigger Lambda. Region pinning gotcha (us-west-2 only). Created Wave 7 (2026-04-26). |
