# SOP — SageMaker HyperPod (Slurm + EKS) for resilient foundation-model training

**Version:** 2.0 · **Last-reviewed:** 2026-04-26 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · SageMaker HyperPod (GA 2024) · Slurm orchestration OR Amazon EKS orchestration · GPU instances (P5/P5e/P5en/P4d) and Trainium2 (trn2.48xlarge) · auto-recovery + checkpoint resumption · NeMo / FSDP / PyTorch DistributedDataParallel · 100B+ parameter foundation models · Llama 3, Mistral, Qwen, custom architectures

---

## 1. Purpose

- Codify the **HyperPod cluster pattern** for resilient training of foundation models (10B - 1T+ parameters), where individual training jobs run for days-to-weeks and individual node failures are common. HyperPod's auto-recovery + checkpoint resumption means a 7-day training run can survive multiple node deaths without restart.
- Codify the **Slurm vs EKS orchestration** decision tree.
- Provide the **GPU + Trainium2** sizing matrix for representative model sizes (7B, 13B, 70B, 175B, 405B, 671B).
- Codify the **resilience features**: deep health checks, node quarantine, auto-resume with checkpoint replay, multi-tenant Kueue queues (EKS).
- Cover the **PEFT-LoRA recipe pattern** (HyperPod ships official recipes for Llama 3 70B/405B and other open-weight FMs).
- Cover the **EFA networking** requirement for multi-node training (without EFA, all-reduce bandwidth dominates and training stalls).
- This is the **FM-training specialisation**. `MLOPS_SAGEMAKER_TRAINING` covers smaller-scale jobs (sub-100B params, single-node, hours-scale). HyperPod is for 100B+ params, multi-node, days-scale.

When the SOW signals: "train a foundation model from scratch", "fine-tune Llama 3 70B/405B", "we have a custom architecture > 30B params", "training run > 24 hours", "we need resilient multi-node training", "Trainium2 cluster".

---

## 2. Decision tree — Slurm vs EKS, plus alternatives

```
Workload type?
├── Pretrain / continue-pretrain a 30B+ model from scratch → §3 HyperPod (Slurm or EKS)
├── PEFT-LoRA fine-tune of an open-weight FM (Llama 3 8B/70B/405B) → §3 HyperPod
├── Full fine-tune of a 7B-13B model on labeled data → MLOPS_SAGEMAKER_TRAINING (single-node OK)
├── Fine-tune via SageMaker JumpStart UI (no infra control) → MLOPS_LLM_FINETUNING_PROD §JumpStart
├── Train < 24 hours single-node → MLOPS_SAGEMAKER_TRAINING (cheaper, simpler)
└── Inference only → MLOPS_SAGEMAKER_SERVING / MLOPS_LLM_FINETUNING_PROD §adapter

Orchestration?
├── Existing Slurm/HPC team, traditional batch scheduling → §3 Slurm orchestration
├── Existing Kubernetes team, want shared cluster + multi-tenant → §4 EKS orchestration
├── No ML infra team, want fully managed → consider SageMaker Training Plans (single-job managed cluster) instead
└── Need GPU governance / queue prioritization (multiple research teams) → §4 EKS w/ Kueue

Compute family?
├── 7B-70B PEFT-LoRA, mostly inference cost-sensitive → §5 Trainium2 (trn2.48xlarge, 4-16 nodes)
├── 70B-405B pretrain or full fine-tune → §3 GPU (P5e/P5en, 8-128 nodes)
├── > 405B (e.g. Llama 4 671B MoE) → P5en clusters with EFA + FSDP, 64+ nodes
└── Cost-sensitive 7B-13B training → P4d (older Hopper) or g5.48xlarge

Resilience requirement?
├── Run will exceed 24 hours → MUST use HyperPod (not regular SageMaker training)
├── Frequent node failures expected (large clusters) → HyperPod with deep health checks
└── Single-node, < 24 hours, idempotent → SageMaker Training Job (simpler)
```

### 2.1 Variant for the engagement (Monolith vs Micro-Stack)

| You are… | Use variant |
|---|---|
| POC — cluster + S3 artifacts + IAM + monitoring all in one stack | **§3 / §4 Monolith Variant** |
| `ClusterStack` owns HyperPod + EKS; `JobsStack` owns IAM job roles + S3 artifact prefixes | **§7 Micro-Stack Variant** |

**Why the split.** A HyperPod cluster is a long-lived expensive resource (running EFA-enabled P5e nodes is ~$30-60/hr/node). Job logic changes daily; cluster lifetime is months. Splitting allows redeploys of job logic without disturbing the cluster.

---

## 3. HyperPod with Slurm orchestration variant

### 3.1 Architecture

```
   ┌──────────────────────────────────────────────────────────────────┐
   │  HyperPod Cluster: fm-training-prod                              │
   │     - Orchestrator: Slurm                                         │
   │     - Head node: 1× m5.4xlarge (Slurm controller + DDB session)   │
   │     - Worker nodes: 32× p5e.48xlarge (8× H200 GPUs each = 256 GPUs)│
   │     - EFA: 32 × 100 Gbps (3.2 Tbps cluster bandwidth)             │
   │     - FSx Lustre: 1.2 PB attached to /fsx (training data + chkpt) │
   │     - SSM Session Manager (no SSH; secure ops access)             │
   └────────────┬─────────────────────────────────────────────────────┘
                │
                ▼
   ┌──────────────────────────────────────────────────────────────────┐
   │  Resilience layer (always-on)                                    │
   │     - Deep health checks (DCGM, NCCL, EFA): every 10 min          │
   │     - On failure: node quarantine + spare swap (< 60s)            │
   │     - Auto-resume: training script reads CHECKPOINT_PATH env       │
   │       resumes from last saved step on new node                     │
   │     - Heartbeat to SSM Cloudwatch agent → CloudWatch alarms       │
   └──────────────────────────────────────────────────────────────────┘
                │
                ▼
   ┌──────────────────────────────────────────────────────────────────┐
   │  Training launch: srun via head node, or SageMaker Recipes        │
   │     - Recipe: hyperpod-recipes/training/llama-3.1-70b-pretrain    │
   │     - launcher: enroot (squash file image)                        │
   │     - Submission: srun -N 32 --gpus=256 ./launch.sh               │
   │     - Monitoring: Slurm sacct + CloudWatch + W&B                  │
   └──────────────────────────────────────────────────────────────────┘
                │
                ▼
   FSx Lustre (1.2 PB) → checkpoints written every 1000 steps
                       → final model weights
                       → data preprocessing outputs
   S3 Backup (lifecycle to Glacier after 30 days)
```

### 3.2 CDK — `_create_hyperpod_slurm_cluster()`

```python
from aws_cdk import (
    Duration, RemovalPolicy, CfnOutput,
    aws_iam as iam,
    aws_kms as kms,
    aws_s3 as s3,
    aws_ec2 as ec2,
    aws_sagemaker as sagemaker,         # L1 only — HyperPod not yet L2 in CDK
    aws_fsx as fsx,
    aws_ssm as ssm,
)


def _create_hyperpod_slurm_cluster(self, stage: str) -> None:
    """Monolith variant. HyperPod Slurm cluster for FM pretrain/fine-tune.
    Creates: cluster + FSx Lustre + IAM + S3 artifacts + monitoring."""

    # A) S3 artifacts bucket — checkpoints overflow + final models
    self.fm_artifacts = s3.Bucket(self, "FMArtifacts",
        bucket_name=f"{{project_name}}-fm-artifacts-{stage}",
        encryption=s3.BucketEncryption.KMS,
        encryption_key=self.kms_key,
        block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
        enforce_ssl=True,
        versioned=True,
        lifecycle_rules=[s3.LifecycleRule(
            id="GlacierAfter30Days",
            transitions=[s3.Transition(
                storage_class=s3.StorageClass.GLACIER_INSTANT_RETRIEVAL,
                transition_after=Duration.days(30),
            )],
            noncurrent_version_expiration=Duration.days(90),
        )],
        removal_policy=RemovalPolicy.RETAIN,
    )

    # B) FSx Lustre — high-throughput parallel file system for training data
    fsx_sg = ec2.SecurityGroup(self, "FsxSg", vpc=self.vpc,
        description="FSx Lustre — accept from cluster nodes only")
    fsx_sg.add_ingress_rule(self.cluster_sg, ec2.Port.tcp_range(988, 1023),
        description="Lustre LNet")

    self.fsx_lustre = fsx.LustreFileSystem(self, "FmFsx",
        vpc=self.vpc,
        vpc_subnet=self.vpc.select_subnets(
            subnet_type=ec2.SubnetType.PRIVATE_ISOLATED).subnets[0],
        security_group=fsx_sg,
        storage_capacity_gib=1228800,                   # 1.2 PB
        lustre_configuration=fsx.LustreConfiguration(
            deployment_type=fsx.LustreDeploymentType.PERSISTENT_2,
            per_unit_storage_throughput=1000,           # MB/s/TiB; PERSISTENT_2 max
            data_compression_type=fsx.LustreDataCompressionType.LZ4,
            # Optional S3 data repository association — auto-import dataset
            # auto_import_policy=fsx.LustreAutoImportPolicy.NEW_CHANGED_DELETED,
        ),
        kms_key=self.kms_key,
        removal_policy=RemovalPolicy.RETAIN,
    )

    # C) Cluster IAM execution role
    self.cluster_role = iam.Role(self, "HyperPodClusterRole",
        assumed_by=iam.ServicePrincipal("sagemaker.amazonaws.com"),
        managed_policies=[
            iam.ManagedPolicy.from_aws_managed_policy_name(
                "AmazonSageMakerFullAccess"),         # narrow this in prod
        ],
        permissions_boundary=self.permission_boundary,
    )
    # S3 / FSx / KMS / SSM access
    self.fm_artifacts.grant_read_write(self.cluster_role)
    self.kms_key.grant_encrypt_decrypt(self.cluster_role)
    self.cluster_role.add_to_policy(iam.PolicyStatement(
        actions=["fsx:DescribeFileSystems", "fsx:DescribeMountTargets"],
        resources=[self.fsx_lustre.file_system_arn],
    ))
    self.cluster_role.add_to_policy(iam.PolicyStatement(
        actions=[
            "ssm:UpdateInstanceInformation",
            "ssm:GetParameters",
            "ssmmessages:CreateControlChannel",
            "ssmmessages:CreateDataChannel",
            "ssmmessages:OpenControlChannel",
            "ssmmessages:OpenDataChannel",
        ],
        resources=["*"],                                # SSM session manager
    ))
    # CloudWatch logs + metrics
    self.cluster_role.add_to_policy(iam.PolicyStatement(
        actions=[
            "logs:CreateLogStream", "logs:PutLogEvents",
            "logs:CreateLogGroup", "logs:DescribeLogGroups",
            "cloudwatch:PutMetricData",
        ],
        resources=["*"],
    ))

    # D) Cluster lifecycle script — bootstraps Slurm + DCGM + Lustre mount
    # Saved to S3, referenced by HyperPod
    bootstrap_script = """#!/bin/bash
set -euxo pipefail

# 1. Mount FSx Lustre at /fsx
sudo mkdir -p /fsx
sudo mount -t lustre {fsx_dns}@tcp:/{fsx_mount} /fsx -o noatime,flock

# 2. Install DCGM exporter for GPU metrics
sudo systemctl start nvidia-dcgm
sudo /opt/ml/scripts/install_dcgm_exporter.sh

# 3. Configure Slurm GRES for GPUs
echo "AccountingStorageTRES=gres/gpu" | sudo tee -a /etc/slurm/slurm.conf

# 4. Sync /opt/ml/scripts from S3
aws s3 sync s3://{{project_name}}-fm-artifacts-{stage}/scripts/ /opt/ml/scripts/

# 5. Start health-check daemon (DCGM + NCCL + EFA)
sudo systemctl start hyperpod-health-check

echo "Bootstrap complete on $(hostname)"
"""

    bootstrap_s3_key = f"scripts/cluster-bootstrap-{stage}.sh"
    s3deploy.BucketDeployment(self, "BootstrapScript",
        sources=[s3deploy.Source.data(bootstrap_s3_key, bootstrap_script)],
        destination_bucket=self.fm_artifacts,
    )

    # E) HyperPod cluster (CfnCluster — L1 only)
    self.hp_cluster = sagemaker.CfnCluster(self, "FmHyperPodCluster",
        cluster_name=f"{{project_name}}-fm-{stage}",
        instance_groups=[
            # Head node (Slurm controller)
            sagemaker.CfnCluster.ClusterInstanceGroupProperty(
                instance_group_name="head",
                instance_type="ml.m5.4xlarge",
                instance_count=1,
                execution_role=self.cluster_role.role_arn,
                threads_per_core=2,
                instance_storage_configs=[],
                life_cycle_config=sagemaker.CfnCluster.ClusterLifeCycleConfigProperty(
                    source_s3_uri=f"s3://{self.fm_artifacts.bucket_name}/scripts/",
                    on_create="cluster-bootstrap-{stage}.sh",
                ),
            ),
            # Worker compute nodes (P5e for FM training)
            sagemaker.CfnCluster.ClusterInstanceGroupProperty(
                instance_group_name="compute",
                instance_type="ml.p5e.48xlarge" if stage == "prod" else "ml.p4d.24xlarge",
                instance_count=32 if stage == "prod" else 4,
                execution_role=self.cluster_role.role_arn,
                threads_per_core=2,
                # P5e nodes come with 30 TB local NVMe — used for /tmp + dataset cache
                instance_storage_configs=[
                    sagemaker.CfnCluster.ClusterInstanceStorageConfigProperty(
                        ebs_volume_config=sagemaker.CfnCluster.ClusterEbsVolumeConfigProperty(
                            volume_size_in_gb=200,
                        ),
                    ),
                ],
                life_cycle_config=sagemaker.CfnCluster.ClusterLifeCycleConfigProperty(
                    source_s3_uri=f"s3://{self.fm_artifacts.bucket_name}/scripts/",
                    on_create="cluster-bootstrap-{stage}.sh",
                ),
            ),
        ],
        orchestrator=sagemaker.CfnCluster.OrchestratorProperty(
            slurm=sagemaker.CfnCluster.SlurmProperty(),
        ),
        vpc_config=sagemaker.CfnCluster.VpcConfigProperty(
            security_group_ids=[self.cluster_sg.security_group_id],
            subnets=[s.subnet_id for s in self.vpc.select_subnets(
                subnet_type=ec2.SubnetType.PRIVATE_ISOLATED).subnets],
        ),
    )

    CfnOutput(self, "ClusterName", value=self.hp_cluster.cluster_name)
    CfnOutput(self, "FsxDns", value=self.fsx_lustre.dns_name)

    # F) Publish to SSM for jobs stack
    ssm.StringParameter(self, "ClusterArnSsm",
        parameter_name=f"/{{project_name}}/{stage}/hyperpod/cluster-arn",
        string_value=self.hp_cluster.attr_cluster_arn)
    ssm.StringParameter(self, "FsxDnsSsm",
        parameter_name=f"/{{project_name}}/{stage}/hyperpod/fsx-dns",
        string_value=self.fsx_lustre.dns_name)
```

### 3.3 Submitting a training job (PEFT-LoRA on Llama 3 70B)

The launcher script invoked by `srun`:

```bash
#!/bin/bash
# scripts/launch_llama3_70b_lora.sh
# Submitted via: srun -N 8 --gpus=64 ./launch_llama3_70b_lora.sh
set -euxo pipefail

# 1. Source NeMo environment (preinstalled by HyperPod recipe)
source /opt/nemo/setup.sh

# 2. Compute node count + rank from Slurm env
NNODES="${SLURM_JOB_NUM_NODES}"
NODE_RANK="${SLURM_NODEID}"
MASTER_ADDR="$(scontrol show hostname "$SLURM_NODELIST" | head -n1)"

# 3. Launch via HyperPod recipe (NeMo + PyTorch FSDP)
recipes-launcher \
    cluster=slurm \
    recipes=hyperpod-recipes/training/llama-3.1-70b-fine-tune \
    base_results_dir=/fsx/results/llama3-70b-lora-$(date +%s) \
    base_model_path=/fsx/checkpoints/llama-3.1-70b-base \
    training_data_dir=/fsx/datasets/instruction-tuning \
    training_config.peft.peft_scheme=lora \
    training_config.peft.lora_tuning.adapter_dim=16 \
    training_config.peft.lora_tuning.alpha=32 \
    training_config.trainer.devices=8 \
    training_config.trainer.num_nodes=${NNODES} \
    training_config.trainer.max_steps=10000 \
    training_config.trainer.val_check_interval=500 \
    training_config.exp_manager.checkpoint_callback_params.save_top_k=3 \
    training_config.exp_manager.checkpoint_callback_params.every_n_train_steps=1000

# 4. After training completes, sync final checkpoints to S3 (Glacier later)
aws s3 sync /fsx/results/llama3-70b-lora-* \
    s3://{{project_name}}-fm-artifacts-{stage}/checkpoints/$(date +%Y%m%d)/
```

Submit:

```bash
ssm start-session --target i-{head_node_id}
sudo -u ml /fsx/scripts/submit_llama3_70b_lora.sh
```

### 3.4 Resilience features (always-on)

| Feature | Behavior | Where to verify |
|---|---|---|
| **Deep health checks** | DCGM (GPU), NCCL (interconnect), EFA (network) probed every 10 min | CloudWatch metric `HealthCheckPass` per node |
| **Auto-recovery** | On 3 consecutive failures, node is quarantined + spare swapped in | Cluster instance group `current_count` should match `target_count` |
| **Checkpoint resume** | Training script reads `CHECKPOINT_PATH` env, resumes from last saved step | NeMo recipes do this automatically |
| **Manual replace** | Operator can `aws sagemaker update-cluster` to force-replace a sticky node | CLI command on head node |
| **Cluster scale-up** | Add more compute nodes via update-cluster + LC config re-run | Health-check passes before joining |

### 3.5 Cost ballpark — typical FM training engagements

| Workload | Cluster | Time | Cost ($/run) |
|---|---|---|---|
| Llama 3 8B PEFT-LoRA on 100K samples | 1 × p4d.24xlarge | 4 hours | ~$130 |
| Llama 3 70B PEFT-LoRA on 1M samples | 8 × p5e.48xlarge | 24 hours | ~$8,500 |
| Llama 3 70B full fine-tune | 32 × p5e.48xlarge | 5 days | ~$170,000 |
| Llama 3 405B PEFT-LoRA | 32 × p5e.48xlarge | 7 days | ~$240,000 |
| Llama 4 671B MoE pretrain (subset) | 128 × p5en.48xlarge | 30 days | ~$5M+ |

(P5e.48xlarge $30.78/hr · P5en $32.24/hr · prices 2026-04, on-demand. Reserved or Capacity Blocks 30-50% cheaper.)

---

## 4. HyperPod with EKS orchestration variant

### 4.1 When to use over Slurm

- Existing Kubernetes platform team — reuse skills + tooling.
- Multi-tenant cluster sharing — multiple research teams compete for GPUs; need quotas + priority.
- Want **Kueue** for batch scheduling with preemption.
- Want to colocate inference workloads on the same cluster (rare; expensive).

### 4.2 CDK delta from Slurm variant

Replace the orchestrator block:

```python
# A) Existing EKS cluster (out-of-band; reference by name)
eks_cluster_name = ssm.StringParameter.value_for_string_parameter(
    self, f"/{{project_name}}/{stage}/eks/cluster-name")

self.hp_cluster = sagemaker.CfnCluster(self, "FmHyperPodEks",
    cluster_name=f"{{project_name}}-fm-eks-{stage}",
    instance_groups=[...],                              # same as §3.2
    orchestrator=sagemaker.CfnCluster.OrchestratorProperty(
        eks=sagemaker.CfnCluster.EksProperty(
            cluster_arn=f"arn:aws:eks:{self.region}:{self.account}:cluster/{eks_cluster_name}",
        ),
    ),
    vpc_config=...,
)

# B) Install Kueue (cluster-side, kubectl)
# kubectl apply -f https://github.com/kubernetes-sigs/kueue/releases/download/v0.10.0/manifests.yaml

# C) Define ClusterQueue + LocalQueue per team (YAML, not CDK)
```

YAML for multi-team queue setup:

```yaml
# k8s/clusterqueue-team-research.yaml
apiVersion: kueue.x-k8s.io/v1beta1
kind: ClusterQueue
metadata:
  name: team-research
spec:
  cohort: gpu-pool
  preemption:
    reclaimWithinCohort: Any
    withinClusterQueue: LowerPriority
  resourceGroups:
  - coveredResources: ["nvidia.com/gpu"]
    flavors:
    - name: "p5e-48xlarge"
      resources:
      - name: "nvidia.com/gpu"
        nominalQuota: 64                 # team allocation
        borrowingLimit: 192              # can borrow from cohort
```

Submit a training job via `kubectl`:

```bash
kubectl apply -f - <<EOF
apiVersion: kubeflow.org/v1
kind: PyTorchJob
metadata:
  name: llama3-70b-lora-$(date +%s)
  namespace: team-research
  labels:
    kueue.x-k8s.io/queue-name: team-research-local
spec:
  pytorchReplicaSpecs:
    Master:
      replicas: 1
      template:
        spec:
          containers:
          - name: pytorch
            image: ${ECR_URI}/nemo-launcher:latest
            command: ["recipes-launcher", "cluster=k8s", "recipes=hyperpod-recipes/training/llama-3.1-70b-fine-tune"]
            resources:
              limits:
                nvidia.com/gpu: 8
    Worker:
      replicas: 7
      template: ...                      # same as Master
EOF
```

---

## 5. Trainium2 variant (cost-efficient FM training)

For 7B-70B PEFT-LoRA workloads, Trainium2 (trn2.48xlarge, $7.78/hr — 75% cheaper than P5e) provides ~80% of P5e's training throughput on PyTorch + Neuron SDK. Best for orgs with cost sensitivity.

```python
# Replace instance_type in compute group
sagemaker.CfnCluster.ClusterInstanceGroupProperty(
    instance_group_name="compute-trn",
    instance_type="ml.trn2.48xlarge",                   # 16 × Trainium2 chips per node
    instance_count=8,
    execution_role=self.cluster_role.role_arn,
    # Trainium uses its own bootstrap (Neuron SDK + neuronx-distributed)
    life_cycle_config=sagemaker.CfnCluster.ClusterLifeCycleConfigProperty(
        source_s3_uri=f"s3://{self.fm_artifacts.bucket_name}/scripts/",
        on_create="trainium-bootstrap-{stage}.sh",      # different bootstrap
    ),
),
```

See `MLOPS_TRAINIUM_INFERENTIA_NEURON` for full Trainium2 setup (Neuron compiler, neuronx-distributed launcher, NxD Training library).

---

## 6. Common gotchas + decisions matrix

| Symptom | Root cause | Fix |
|---|---|---|
| "Cluster CREATING for > 90 min" | LC script failing | SSM-session into head node, `journalctl -u cloud-final`. Common: FSx mount permission issue |
| Training stalls at 100% GPU but 0% throughput | NCCL not using EFA | Verify `FI_PROVIDER=efa` env; ensure cluster SG opens UDP 1024-65535 between worker nodes |
| OOM during PEFT-LoRA on 70B | Activation checkpointing off | NeMo recipes default to `activations_checkpoint_granularity=full`. Reduce `micro_batch_size` to 1 |
| Job dies after 24h with no error | Slurm `TimeLimit=1-00:00:00` (1 day default) | Set `TimeLimit=14-00:00:00` (14 days); also adjust SGE if used |
| Checkpoint corrupted on resume | Async S3 sync interrupted | Enable atomic writes — write to `<dir>.tmp/` then rename. Use NeMo's built-in atomic checkpointing |
| Node deemed unhealthy but actually fine | DCGM probe transient failure | Tune `health_check_threshold` to 5 consecutive failures (default 3) |
| EFA not available in chosen AZ | Capacity / region mismatch | P5e/P5en require Capacity Reservations or specific AZs; check via `aws ec2 describe-instance-type-offerings` |
| FSx Lustre throughput cap | Storage not provisioned for throughput | PERSISTENT_2 = 1000 MB/s/TiB; for 12 GB/s need ≥ 12 TiB. Bump `storage_capacity_gib` |
| Trainium kernels recompile on each run | Neuron compile cache not persisted | Set `NEURON_COMPILE_CACHE_URL=s3://<bucket>/neuron-cache/` |

### 6.1 GPU vs Trainium2 vs alternatives

| Workload | GPU (P5e) | Trainium2 | Alternative |
|---|---|---|---|
| Llama 3 7B fine-tune | $30/hr × 1 = $30/hr | $8/hr × 1 = $8/hr | Bedrock JumpStart hosted FT (no infra) |
| Llama 3 70B fine-tune | $30/hr × 8 = $240/hr | $8/hr × 16 = $128/hr | — |
| Llama 4 405B pretrain | $30/hr × 64 = $1,920/hr | Not yet supported on Trainium | — |
| 1T+ MoE pretrain | $32/hr × 128 = $4,100/hr | Not yet supported | — |
| Inference (after FT) | g5/g6 endpoint (~$5/hr) | inf2.48xlarge ($12/hr — 6× chips) | Bedrock-hosted post-training |

---

## 7. Micro-Stack variant (cross-stack via SSM)

```python
# In ClusterStack
ssm.StringParameter(self, "ClusterName",
    parameter_name=f"/{{project_name}}/{stage}/hyperpod/cluster-name",
    string_value=self.hp_cluster.cluster_name)
ssm.StringParameter(self, "FsxDns",
    parameter_name=f"/{{project_name}}/{stage}/hyperpod/fsx-dns",
    string_value=self.fsx_lustre.dns_name)
ssm.StringParameter(self, "ArtifactsBucket",
    parameter_name=f"/{{project_name}}/{stage}/hyperpod/artifacts-bucket",
    string_value=self.fm_artifacts.bucket_name)

# In JobsStack — submission Lambda + monitoring
cluster_name = ssm.StringParameter.value_for_string_parameter(
    self, f"/{{project_name}}/{stage}/hyperpod/cluster-name")
artifacts = ssm.StringParameter.value_for_string_parameter(
    self, f"/{{project_name}}/{stage}/hyperpod/artifacts-bucket")

# Submission Lambda — uses SSM to invoke srun on head node
submit_fn = lambda_.Function(self, "SubmitJob",
    runtime=lambda_.Runtime.PYTHON_3_12,
    handler="index.handler",
    code=lambda_.Code.from_asset(str(LAMBDA_SRC / "submit_hyperpod_job")),
    timeout=Duration.minutes(5),
    environment={"CLUSTER_NAME": cluster_name, "BUCKET": artifacts},
)
submit_fn.add_to_role_policy(iam.PolicyStatement(
    actions=["sagemaker:DescribeCluster", "sagemaker:DescribeClusterNode"],
    resources=[f"arn:aws:sagemaker:{self.region}:{self.account}:cluster/{cluster_name}"],
))
submit_fn.add_to_role_policy(iam.PolicyStatement(
    actions=["ssm:SendCommand"],
    resources=[f"arn:aws:ssm:{self.region}:{self.account}:document/AWS-RunShellScript",
               f"arn:aws:ec2:{self.region}:{self.account}:instance/*"],
))
```

---

## 8. Worked example — pytest synth

```python
def test_hyperpod_slurm_synthesizes():
    app = cdk.App()
    env = cdk.Environment(account="000000000000", region="us-east-1")
    deps = cdk.Stack(app, "Deps", env=env)
    vpc = ec2.Vpc(deps, "Vpc", max_azs=2,
        subnet_configuration=[ec2.SubnetConfiguration(
            name="iso", subnet_type=ec2.SubnetType.PRIVATE_ISOLATED, cidr_mask=24)])
    cluster_sg = ec2.SecurityGroup(deps, "CSg", vpc=vpc)
    key = kms.Key(deps, "Key")
    boundary = iam.ManagedPolicy(deps, "B", statements=[
        iam.PolicyStatement(actions=["*"], resources=["*"])])

    from infrastructure.cdk.stacks.hyperpod_slurm_stack import HyperPodSlurmStack
    stack = HyperPodSlurmStack(app, stage_name="dev",
        vpc=vpc, cluster_sg=cluster_sg, kms_key=key,
        permission_boundary=boundary, env=env)
    t = Template.from_stack(stack)

    # HyperPod cluster
    t.has_resource_properties("AWS::SageMaker::Cluster", Match.object_like({
        "Orchestrator": Match.object_like({
            "Slurm": Match.any_value(),
        }),
        "InstanceGroups": Match.array_with([
            Match.object_like({
                "InstanceGroupName": "head",
                "InstanceCount": 1,
            }),
            Match.object_like({
                "InstanceGroupName": "compute",
                "InstanceCount": Match.greater_than_or_equal(1),
            }),
        ]),
    }))
    # FSx Lustre
    t.has_resource_properties("AWS::FSx::FileSystem", Match.object_like({
        "FileSystemType": "LUSTRE",
        "LustreConfiguration": Match.object_like({
            "DeploymentType": "PERSISTENT_2",
            "PerUnitStorageThroughput": 1000,
            "DataCompressionType": "LZ4",
        }),
    }))
    # KMS-encrypted artifacts bucket
    t.has_resource_properties("AWS::S3::Bucket", Match.object_like({
        "BucketEncryption": Match.object_like({
            "ServerSideEncryptionConfiguration": Match.array_with([
                Match.object_like({"ServerSideEncryptionByDefault": Match.object_like({
                    "SSEAlgorithm": "aws:kms",
                })}),
            ]),
        }),
    }))
```

---

## 9. Five non-negotiables

1. **Always use FSx Lustre, never EBS-only for training data.** A 100B model checkpoint is 200-400 GB; reading from S3 is 100-1000× slower than Lustre at peak. Throughput cap = your training cap.

2. **EFA must be enabled cluster-wide.** Without EFA, NCCL all-reduce uses TCP — bandwidth drops 10×. P5e/P5en/Trn2 instances all have EFA; just verify `FI_PROVIDER=efa` is set in launch script env.

3. **Checkpoints every 1000 steps minimum, atomic writes mandatory.** Training jobs DIE. If your checkpoint frequency is once-per-epoch, a node failure at 80% through an epoch is 20+ hours wasted.

4. **`removal_policy=RemovalPolicy.RETAIN` on FSx + S3 artifacts.** A single training run is $50K-$200K of compute. Accidental cleanup = $50K-$200K loss.

5. **Cost guard: cluster auto-stop after N hours idle.** HyperPod doesn't auto-stop. A forgotten cluster running 32 P5e nodes burns $24K/day. Wire a CloudWatch alarm on `ActiveJobs=0` for 8h → SNS → manual stop OR Lambda-triggered `update-cluster` to scale compute group to 0.

---

## 10. References

- `docs/template_params.md` — `HYPERPOD_ORCHESTRATOR`, `HYPERPOD_HEAD_INSTANCE`, `HYPERPOD_COMPUTE_INSTANCE`, `HYPERPOD_NODE_COUNT`, `FSX_STORAGE_GIB`, `FSX_THROUGHPUT_PER_TIB_MBPS`
- AWS docs:
  - [HyperPod overview](https://docs.aws.amazon.com/sagemaker/latest/dg/sagemaker-hyperpod.html)
  - [Cluster resiliency (Slurm)](https://docs.aws.amazon.com/sagemaker/latest/dg/sagemaker-hyperpod-resiliency-slurm.html)
  - [HyperPod with EKS orchestration](https://docs.aws.amazon.com/sagemaker/latest/dg/sagemaker-hyperpod-eks.html)
  - [Elastic training](https://docs.aws.amazon.com/sagemaker/latest/dg/sagemaker-eks-elastic-training.html)
  - [Llama 3 70B PEFT-LoRA tutorial](https://docs.aws.amazon.com/sagemaker/latest/dg/sagemaker-eks-checkpointless-recipes-peft-llama.html)
  - [GPU Slurm pretrain tutorial](https://docs.aws.amazon.com/sagemaker/latest/dg/hyperpod-gpu-slurm-pretrain-tutorial.html)
- Related SOPs:
  - `MLOPS_SAGEMAKER_TRAINING` — non-HyperPod single-job training
  - `MLOPS_LLM_FINETUNING_PROD` — JumpStart UI fine-tuning + adapter inference
  - `MLOPS_DISTRIBUTED_TRAINING` — SMDDP + SMP for non-HyperPod multi-GPU
  - `MLOPS_TRAINIUM_INFERENTIA_NEURON` — Trainium2 cluster setup
  - `LAYER_NETWORKING` — VPC + EFA + cluster placement group

---

## 11. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-26 | Initial partial — HyperPod Slurm + EKS for FM training. CDK for cluster + FSx Lustre + IAM + bootstrap LC. PEFT-LoRA Llama 3 70B launcher example with NeMo recipes. EKS variant with Kueue queues. Trainium2 cost variant. Resilience features (deep health checks, auto-recovery, checkpoint resume). Cost ballpark per FM size. 5 non-negotiables. Created to fill F369 audit gap (2026-04-26): FM training was 0% covered despite being top revenue driver. |
