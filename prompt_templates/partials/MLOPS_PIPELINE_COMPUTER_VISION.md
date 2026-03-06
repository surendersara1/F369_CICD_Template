# PARTIAL: Computer Vision Pipeline — Image Classification, Object Detection, Segmentation

**Usage:** Include when SOW mentions image analysis, object detection, quality inspection, medical imaging, document OCR, video analysis, or visual QA/QC.

---

## CV Task → Model Selection

| Task                             | Model                                 | GPU Instance    |
| -------------------------------- | ------------------------------------- | --------------- |
| Image Classification             | ResNet-50, EfficientNet-B4, ViT       | ml.g4dn.xlarge  |
| Object Detection                 | YOLOv8, Detectron2 Faster-RCNN        | ml.g4dn.2xlarge |
| Instance Segmentation            | Mask R-CNN, SAM (Meta)                | ml.g4dn.4xlarge |
| Medical Imaging                  | nnU-Net, Med-SAM                      | ml.g5.2xlarge   |
| Document OCR + Layout            | Amazon Textract (managed), LayoutLMv3 | API / ml.g4dn   |
| Video Frame Analysis             | VideoMAE, X3D                         | ml.g5.xlarge    |
| Defect Detection (manufacturing) | Anomalib, PatchCore                   | ml.g4dn.xlarge  |

---

## CDK Code Block

```python
def _create_computer_vision_pipeline(self, stage_name: str) -> None:
    """
    Computer Vision ML Pipeline.

    Pipeline Steps:
      1. Image Preprocessing — resize, normalize, augment, convert to RecordIO/TFRecord
      2. Model Training — GPU-based training with SageMaker
      3. Model Evaluation — precision, recall, mAP (for detection), IoU (for segmentation)
      4. Model Registration + Endpoint Deployment
      5. Async Inference Configuration (for large images / video frames)
    """

    import aws_cdk.aws_sagemaker as sagemaker

    # =========================================================================
    # CV ASYNC INFERENCE ENDPOINT
    # (Async = better for large images, video frames, batch OCR)
    # =========================================================================

    # Output bucket for async inference results
    async_output_bucket = s3.Bucket(
        self, "CVAsyncOutput",
        bucket_name=f"{{project_name}}-cv-async-output-{stage_name}-{self.account}",
        block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
        encryption=s3.BucketEncryption.KMS,
        encryption_key=self.kms_key,
        lifecycle_rules=[s3.LifecycleRule(expiration=Duration.days(7), enabled=True)],
        removal_policy=RemovalPolicy.DESTROY,
    )

    # Async endpoint config
    cv_endpoint_config = sagemaker.CfnEndpointConfig(
        self, "CVEndpointConfig",
        endpoint_config_name=f"{{project_name}}-cv-{stage_name}",
        async_inference_config=sagemaker.CfnEndpointConfig.AsyncInferenceConfigProperty(
            client_config=sagemaker.CfnEndpointConfig.AsyncInferenceClientConfigProperty(
                max_concurrent_invocations_per_instance=4,  # 4 parallel image inferences
            ),
            output_config=sagemaker.CfnEndpointConfig.AsyncInferenceOutputConfigProperty(
                s3_output_path=f"s3://{async_output_bucket.bucket_name}/results/",
                kms_key_id=self.kms_key.key_arn,
                notification_config=sagemaker.CfnEndpointConfig.AsyncInferenceNotificationConfigProperty(
                    success_topic=self.alert_topic.topic_arn,  # Notify on completion
                    error_topic=self.alert_topic.topic_arn,    # Notify on failure
                ),
            ),
        ),
        production_variants=[
            sagemaker.CfnEndpointConfig.ProductionVariantProperty(
                variant_name="AllTraffic",
                model_name=f"{{project_name}}-cv-model-{stage_name}",
                instance_type="ml.g4dn.xlarge" if stage_name != "prod" else "ml.g4dn.2xlarge",
                initial_instance_count=1,
                managed_instance_scaling=sagemaker.CfnEndpointConfig.ManagedInstanceScalingProperty(
                    status="ENABLED",
                    min_instance_count=1,
                    max_instance_count=4,
                ),
            )
        ],
        kms_key_id=self.kms_key.key_arn,
    )

    # =========================================================================
    # IMAGE SUBMISSION LAMBDA — Async inference submission
    # =========================================================================

    cv_submit_fn = _lambda.Function(
        self, "CVInferenceFn",
        function_name=f"{{project_name}}-cv-inference-{stage_name}",
        runtime=_lambda.Runtime.PYTHON_3_12,
        architecture=_lambda.Architecture.ARM_64,
        handler="index.handler",
        code=_lambda.Code.from_inline("""
import boto3, os, json, uuid, logging
logger = logging.getLogger()
sm_runtime = boto3.client('sagemaker-runtime')
s3 = boto3.client('s3')

ENDPOINT_NAME = os.environ['ENDPOINT_NAME']
INPUT_BUCKET  = os.environ['INPUT_BUCKET']

def handler(event, context):
    image_s3_uri = event.get('image_s3_uri')
    image_base64 = event.get('image_base64')

    # If image sent directly, upload to S3 first (async needs S3 input)
    if image_base64:
        import base64
        key = f"cv-inputs/{uuid.uuid4()}.jpg"
        s3.put_object(Bucket=INPUT_BUCKET, Key=key, Body=base64.b64decode(image_base64))
        image_s3_uri = f"s3://{INPUT_BUCKET}/{key}"

    # Submit async inference — returns immediately with inference ID
    response = sm_runtime.invoke_endpoint_async(
        EndpointName=ENDPOINT_NAME,
        InputLocation=image_s3_uri,    # S3 URI of the image
        ContentType='application/x-image',
        Accept='application/json',
        InferenceId=str(uuid.uuid4()), # Unique ID to correlate result
    )
    return {
        'statusCode': 202,  # Accepted — not yet complete
        'inference_id': response['InferenceId'],
        'output_location': response['OutputLocation'],  # Poll this S3 URI for result
    }
"""),
        environment={
            "ENDPOINT_NAME": f"{{project_name}}-cv-{stage_name}",
            "INPUT_BUCKET":  self.lake_buckets["raw"].bucket_name,
        },
        memory_size=256,
        timeout=Duration.seconds(30),
        tracing=_lambda.Tracing.ACTIVE,
    )
    cv_submit_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["sagemaker:InvokeEndpointAsync"],
        resources=[f"arn:aws:sagemaker:{self.region}:{self.account}:endpoint/{{project_name}}-cv-{stage_name}"],
    ))
    self.lake_buckets["raw"].grant_read_write(cv_submit_fn)

    CfnOutput(self, "CVInferenceFnArn",
        value=cv_submit_fn.function_arn,
        description="Submit image for CV inference (async)",
        export_name=f"{{project_name}}-cv-inference-{stage_name}",
    )
```

---

## Training Script Skeleton (`ml/scripts/cv_train.py`)

```python
# ml/scripts/cv_train.py — Example: YOLOv8 object detection on SageMaker
import subprocess, sys
subprocess.check_call([sys.executable, "-m", "pip", "install", "ultralytics", "-q"])

from ultralytics import YOLO
import argparse, os

def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--model",      default="yolov8n.pt")  # nano=fast, x=accurate
    p.add_argument("--epochs",     type=int, default=100)
    p.add_argument("--imgsz",      type=int, default=640)
    p.add_argument("--batch",      type=int, default=16)
    p.add_argument("--device",     default="0")            # GPU 0
    p.add_argument("--data",       default="/opt/ml/input/data/train/dataset.yaml")
    p.add_argument("--output_dir", default="/opt/ml/model")
    p.add_argument("--project",    default="/opt/ml/output")
    return p.parse_args()

def main():
    args = parse_args()
    model = YOLO(args.model)
    results = model.train(
        data=args.data,
        epochs=args.epochs,
        imgsz=args.imgsz,
        batch=args.batch,
        device=args.device,
        project=args.project,
        patience=20,              # Early stopping
        save=True,
        plots=True,               # Saves confusion matrix, PR curve
        augment=True,             # Random flips, mosaic, mixup
    )
    # Export to ONNX for SageMaker serving (faster than PyTorch)
    model.export(format="onnx", dynamic=True, simplify=True)

if __name__ == "__main__":
    main()
```
