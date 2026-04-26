# SOP — SageMaker Studio Spaces (per-user JupyterLab + Code Editor + lifecycle configs + custom images)

**Version:** 2.0 · **Last-reviewed:** 2026-04-26 · **Status:** Active
**Applies to:** AWS CDK v2 (Python 3.12+) · SageMaker Studio domains · Studio Spaces (private + shared) · JupyterLab v4 + Code Editor (VSCode-based) + RStudio · Studio Lifecycle Configurations · Custom Studio Images via ECR · per-space EBS · Studio domain default user settings

---

## 1. Purpose

- Codify the **Studio Spaces pattern** — the 2024+ replacement for Studio Apps, where each user gets isolated JupyterLab/Code Editor/RStudio with its own EBS volume, custom image, lifecycle script, and IAM scope.
- Codify the **Lifecycle Configuration** patterns: install company packages, mount FSx, configure git credentials, set proxies, install dotfiles.
- Codify the **Custom Image** pattern: build Docker image with org-specific dependencies, push to ECR, attach to Studio domain.
- Distinguish **private spaces** (1-user) vs **shared spaces** (multi-user collaborative).
- This is the **Studio user-experience specialisation**. `MLOPS_SAGEMAKER_TRAINING` covers the Studio domain itself; this partial covers the per-user inside the domain.

When the SOW signals: "data scientists need their own dev env", "we need company-specific Python packages preinstalled", "users want VSCode not just Jupyter", "RStudio for our analysts", "lifecycle scripts for security setup".

---

## 2. Decision tree

```
Workspace type per user?
├── Solo data science / ML dev → §3 Private Space (default)
├── Pair-programming / collaborative debugging → §4 Shared Space
└── No persistent state (ephemeral notebook) → JupyterLab default app (no space)

App type?
├── Notebooks + Python → JupyterLab (most common)
├── Code editing / Git workflow → Code Editor (VSCode-based)
├── R analytics → RStudio
└── ML pipeline UI → SageMaker Pipelines (built-in)

Customization?
├── Use AWS-provided images + default config → §3 minimal setup
├── Need company packages preinstalled → §5 Custom Image
├── Need post-startup scripts (clone repo, mount FSx) → §6 Lifecycle Config
└── Both (image + LCC) → §5 + §6
```

---

## 3. Private Space variant

### 3.1 Architecture

```
   ┌──────────────────────────────────────────────────────────────────┐
   │  SageMaker Studio Domain                                         │
   │     - Auth: SSO (IAM Identity Center) or IAM                      │
   │     - VPC-only network access                                      │
   │     - Default execution role + per-user role overrides             │
   └──────────────────┬───────────────────────────────────────────────┘
                      │
                      ▼
   ┌──────────────────────────────────────────────────────────────────┐
   │  User Profile: maria@example.com                                 │
   │     - Execution role: maria-data-scientist-role                    │
   │     - Default user settings (image, instance, LCC)                 │
   └──────────────────┬───────────────────────────────────────────────┘
                      │
                      ├──── Private Space: maria-jupyter-prod ─────┐
                      │     - JupyterLab v4                                                            │
                      │     - Instance: ml.t3.medium → ml.g5.4xlarge                                    │
                      │     - EBS: 100 GB persistent (per-space)                                        │
                      │     - LCC: install company-prebuilt.lcc                                         │
                      │     - Image: company-ml-image:latest (custom from ECR)                          │
                      │
                      └──── Private Space: maria-codeeditor-prod ──┐
                            - Code Editor (VSCode-based)                                                │
                            - Same instance + EBS + LCC                                                 │
                            - Git config + dotfiles auto-loaded                                         │
```

### 3.2 CDK — `_create_user_profile_with_space()`

```python
from aws_cdk import (
    aws_iam as iam,
    aws_sagemaker as sagemaker,
    aws_ec2 as ec2,
)


def _create_user_profile_with_space(self, stage: str) -> None:
    """Creates user profile + private space within an existing Studio domain."""

    # A) Per-user execution role (least privilege)
    user_role = iam.Role(self, "UserRole",
        role_name=f"{{project_name}}-maria-{stage}",
        assumed_by=iam.ServicePrincipal("sagemaker.amazonaws.com"),
        managed_policies=[
            iam.ManagedPolicy.from_aws_managed_policy_name(
                "AmazonSageMakerCanvasFullAccess"),         # adjust per role
        ],
        permissions_boundary=self.permission_boundary,
    )
    self.training_input.grant_read(user_role)
    self.kms_key.grant_encrypt_decrypt(user_role)

    # B) User profile
    user_profile = sagemaker.CfnUserProfile(self, "MariaProfile",
        domain_id=self.studio_domain.attr_domain_id,
        user_profile_name="maria",
        user_settings=sagemaker.CfnUserProfile.UserSettingsProperty(
            execution_role=user_role.role_arn,
            sharing_settings=sagemaker.CfnUserProfile.SharingSettingsProperty(
                notebook_output_option="Allowed",
                s3_kms_key_id=self.kms_key.key_arn,
            ),
            default_landing_uri="studio::",
            studio_web_portal="ENABLED",
            jupyter_lab_app_settings=sagemaker.CfnUserProfile.JupyterLabAppSettingsProperty(
                default_resource_spec=sagemaker.CfnUserProfile.ResourceSpecProperty(
                    instance_type="ml.t3.medium",
                    sage_maker_image_arn=f"arn:aws:sagemaker:{self.region}:{self.account}:image/company-ml-image:1",
                    lifecycle_config_arn=self.user_lcc.attr_studio_lifecycle_config_arn,
                ),
            ),
            code_editor_app_settings=sagemaker.CfnUserProfile.CodeEditorAppSettingsProperty(
                default_resource_spec=sagemaker.CfnUserProfile.ResourceSpecProperty(
                    instance_type="ml.t3.medium",
                    lifecycle_config_arn=self.user_lcc.attr_studio_lifecycle_config_arn,
                ),
            ),
        ),
    )

    # C) Private Space — JupyterLab
    sagemaker.CfnSpace(self, "MariaJupyterSpace",
        domain_id=self.studio_domain.attr_domain_id,
        space_name="maria-jupyter-prod",
        ownership_settings=sagemaker.CfnSpace.OwnershipSettingsProperty(
            owner_user_profile_name=user_profile.user_profile_name,
        ),
        space_settings=sagemaker.CfnSpace.SpaceSettingsProperty(
            app_type="JupyterLab",
            jupyter_lab_app_settings=sagemaker.CfnSpace.SpaceJupyterLabAppSettingsProperty(
                default_resource_spec=sagemaker.CfnSpace.ResourceSpecProperty(
                    instance_type="ml.g5.4xlarge",        # GPU for ML dev
                    sage_maker_image_arn=f"arn:aws:sagemaker:{self.region}:{self.account}:image/company-ml-image:1",
                    lifecycle_config_arn=self.user_lcc.attr_studio_lifecycle_config_arn,
                ),
                code_repositories=[sagemaker.CfnSpace.CodeRepositoryProperty(
                    repository_url="https://github.com/example/ml-platform.git",
                )],
            ),
            space_storage_settings=sagemaker.CfnSpace.SpaceStorageSettingsProperty(
                ebs_storage_settings=sagemaker.CfnSpace.EbsStorageSettingsProperty(
                    ebs_volume_size_in_gb=100,           # persistent across restarts
                ),
            ),
        ),
        space_sharing_settings=sagemaker.CfnSpace.SpaceSharingSettingsProperty(
            sharing_type="Private",
        ),
    )

    # D) Private Space — Code Editor
    sagemaker.CfnSpace(self, "MariaCodeEditorSpace",
        domain_id=self.studio_domain.attr_domain_id,
        space_name="maria-codeeditor-prod",
        ownership_settings=sagemaker.CfnSpace.OwnershipSettingsProperty(
            owner_user_profile_name=user_profile.user_profile_name,
        ),
        space_settings=sagemaker.CfnSpace.SpaceSettingsProperty(
            app_type="CodeEditor",
            code_editor_app_settings=sagemaker.CfnSpace.SpaceCodeEditorAppSettingsProperty(
                default_resource_spec=sagemaker.CfnSpace.ResourceSpecProperty(
                    instance_type="ml.t3.large",
                    lifecycle_config_arn=self.user_lcc.attr_studio_lifecycle_config_arn,
                ),
            ),
            space_storage_settings=sagemaker.CfnSpace.SpaceStorageSettingsProperty(
                ebs_storage_settings=sagemaker.CfnSpace.EbsStorageSettingsProperty(
                    ebs_volume_size_in_gb=50,
                ),
            ),
        ),
        space_sharing_settings=sagemaker.CfnSpace.SpaceSharingSettingsProperty(
            sharing_type="Private",
        ),
    )
```

---

## 4. Shared Space variant

```python
sagemaker.CfnSpace(self, "TeamCollabSpace",
    domain_id=self.studio_domain.attr_domain_id,
    space_name="ml-team-collab",
    ownership_settings=sagemaker.CfnSpace.OwnershipSettingsProperty(
        owner_user_profile_name="ml-team-lead",
    ),
    space_settings=sagemaker.CfnSpace.SpaceSettingsProperty(
        app_type="JupyterLab",
        jupyter_lab_app_settings=sagemaker.CfnSpace.SpaceJupyterLabAppSettingsProperty(
            default_resource_spec=sagemaker.CfnSpace.ResourceSpecProperty(
                instance_type="ml.g5.12xlarge",
            ),
        ),
    ),
    space_sharing_settings=sagemaker.CfnSpace.SpaceSharingSettingsProperty(
        sharing_type="Shared",                            # Multi-user
    ),
)
```

Members of the shared space access via `aws sagemaker create-presigned-domain-url --space-name ml-team-collab`. Useful for paired debugging or office-hours-style collaboration.

---

## 5. Custom Image variant — company packages preinstalled

### 5.1 Build the custom image

```dockerfile
# Dockerfile.studio-image
FROM public.ecr.aws/sagemaker/sagemaker-distribution:2.0-cpu

USER root

# Company packages
RUN pip install --no-cache-dir \
    pandas==2.2.* \
    numpy==1.26.* \
    scikit-learn==1.4.* \
    transformers==4.45.0 \
    company-internal-data-lib==3.0.0 \
    company-internal-ml-utils==2.1.0

# Company CA cert
COPY company-root-ca.pem /etc/ssl/certs/
RUN update-ca-certificates

# Default git config
RUN git config --system user.name "Company ML User" && \
    git config --system core.editor "code --wait"

USER 1000
```

Build + push:

```bash
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin 111111111111.dkr.ecr.us-east-1.amazonaws.com
docker build -t company-ml-image:1 -f Dockerfile.studio-image .
docker tag company-ml-image:1 111111111111.dkr.ecr.us-east-1.amazonaws.com/company-ml-image:1
docker push 111111111111.dkr.ecr.us-east-1.amazonaws.com/company-ml-image:1
```

### 5.2 Register with Studio

```python
# Image
custom_image = sagemaker.CfnImage(self, "CompanyMlImage",
    image_name="company-ml-image",
    image_role_arn=image_role.role_arn,
    image_description="Company-internal ML image with prebuilt packages",
)

# ImageVersion
custom_image_v1 = sagemaker.CfnImageVersion(self, "CompanyMlImageV1",
    image_name=custom_image.image_name,
    base_image=f"111111111111.dkr.ecr.{self.region}.amazonaws.com/company-ml-image:1",
)

# AppImageConfig — tells Studio how to use it
sagemaker.CfnAppImageConfig(self, "CompanyMlImageConfig",
    app_image_config_name="company-ml-config",
    kernel_gateway_image_config=sagemaker.CfnAppImageConfig.KernelGatewayImageConfigProperty(
        kernel_specs=[sagemaker.CfnAppImageConfig.KernelSpecProperty(
            name="python3",
            display_name="Python 3 (Company ML)",
        )],
        file_system_config=sagemaker.CfnAppImageConfig.FileSystemConfigProperty(
            mount_path="/home/sagemaker-user",
            default_uid=1000,
            default_gid=100,
        ),
    ),
)

# Attach to Studio domain
self.studio_domain.add_property_override(
    "DefaultUserSettings.JupyterLabAppSettings.CustomImages",
    [{
        "ImageName":             custom_image.image_name,
        "ImageVersionNumber":    1,
        "AppImageConfigName":    "company-ml-config",
    }],
)
```

---

## 6. Lifecycle Configuration variant — startup scripts

### 6.1 LCC content

```bash
#!/bin/bash
# scripts/jupyter-lcc.sh — runs on every Space start

set -eux

# 1. Install company-internal pip dependencies
pip install --quiet \
    company-internal-data-lib \
    company-internal-ml-utils

# 2. Mount FSx for company shared datasets (read-only)
sudo mkdir -p /fsx-shared
sudo mount -t lustre fs-1234567@tcp:/abc -o ro /fsx-shared

# 3. Clone company-internal git repos via Git Credentials Helper
git config --global credential.helper '!aws codecommit credential-helper $@'
git config --global credential.UseHttpPath true
mkdir -p ~/repos
cd ~/repos
git clone https://git-codecommit.us-east-1.amazonaws.com/v1/repos/ml-platform || true

# 4. Set proxy for outbound HTTP (corporate firewall)
echo "export HTTP_PROXY=http://proxy.example.com:8080" >> ~/.bashrc
echo "export HTTPS_PROXY=http://proxy.example.com:8080" >> ~/.bashrc

# 5. Configure Bedrock access via cross-account assume role
aws configure set role_arn "arn:aws:iam::222222222222:role/CompanyBedrockUser" --profile bedrock
aws configure set source_profile "default" --profile bedrock

echo "LCC complete"
```

### 6.2 Register the LCC

```python
import base64

with open("scripts/jupyter-lcc.sh", "r") as f:
    script_content = f.read()
encoded_script = base64.b64encode(script_content.encode()).decode()

self.user_lcc = sagemaker.CfnStudioLifecycleConfig(self, "UserLCC",
    studio_lifecycle_config_name=f"{{project_name}}-jupyter-lcc-{stage}",
    studio_lifecycle_config_app_type="JupyterLab",
    studio_lifecycle_config_content=encoded_script,
)

# Reference from User Profile / Space resource_spec.lifecycle_config_arn (see §3.2)
```

---

## 7. Five non-negotiables

1. **Per-user execution role, not shared default.** A shared role for all users blocks per-user S3 prefix isolation. Always create a per-user role.

2. **`sharing_type="Private"` unless explicitly collaborative.** Shared spaces leak data between users on the same EBS — only use for known-good collaboration.

3. **EBS volume size at least 50 GB.** JupyterLab + Conda envs + git checkout exhaust 25 GB quickly. 100 GB is safer for ML dev.

4. **Lifecycle scripts MUST exit 0 in < 5 min.** Studio kills LCCs after 5 min. Long installs go in the custom image instead.

5. **Image versioning is permanent.** `ImageVersion 1` is immutable. Bump to v2 for changes. Never edit-in-place — Studio caches aggressively.

---

## 8. References

- AWS docs:
  - [Studio Spaces overview](https://docs.aws.amazon.com/sagemaker/latest/dg/studio-updated-jl.html)
  - [Custom Studio Images](https://docs.aws.amazon.com/sagemaker/latest/dg/studio-byoi.html)
  - [Studio Lifecycle Configurations](https://docs.aws.amazon.com/sagemaker/latest/dg/studio-lcc.html)
  - [Code Editor in Studio](https://docs.aws.amazon.com/sagemaker/latest/dg/code-editor.html)
- Related SOPs:
  - `MLOPS_SAGEMAKER_TRAINING` — Studio domain setup (the parent of spaces)
  - `MLOPS_SAGEMAKER_UNIFIED_STUDIO` — modern Unified Studio replacement
  - `LAYER_SECURITY` — IAM permission boundary for user roles

---

## 9. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-26 | Initial partial — Studio Spaces (private + shared) + Custom Studio Images + Lifecycle Configurations. CDK monolith. Created Wave 7 (2026-04-26). |
