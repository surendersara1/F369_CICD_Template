# PARTIAL: MLOps Data Platform — Glue, Athena, Lake Formation, Redshift, EMR

**Usage:** Include when SOW mentions data science, ML, feature engineering, analytics, data warehouse, or data lake.

---

## The ML Data Platform Stack

ML models are only as good as their data. This layer is the **foundation everything else sits on**.
Without it, data scientists are manually downloading CSVs — not doing production ML.

```
Raw Sources                 Ingestion              Storage              Query/Analysis
─────────────              ──────────             ─────────            ──────────────
S3 raw data         ──►   Glue ETL Jobs    ──►   S3 Data Lake  ──►   Athena SQL
Kinesis streams     ──►   Glue Streaming   ──►   Glue Catalog  ──►   SageMaker Studio
RDS/Aurora          ──►   Glue Connectors  ──►   Redshift DW   ──►   QuickSight BI
DynamoDB Streams    ──►   EMR Serverless   ──►   Feature Store ──►   Jupyter Notebooks
External APIs       ──►   Lambda ETL       ──►   Delta/Iceberg ──►   Spark on EMR
```

---

## CDK Code Block — ML Data Platform

```python
def _create_ml_data_platform(self, stage_name: str) -> None:
    """
    Layer 2+: ML Data Platform

    Components:
      A) S3 Data Lake (raw / processed / curated / features zones)
      B) AWS Glue (ETL jobs, Data Catalog, crawlers)
      C) Amazon Athena (serverless SQL on S3)
      D) AWS Lake Formation (data governance, column-level security)
      E) Amazon Redshift Serverless (data warehouse for BI/reporting)
      F) EMR Serverless (large-scale Spark feature engineering)

    [Claude: include only the components detected in the Architecture Map.
     At minimum include A + B + C for any ML/data science SOW.]
    """

    import aws_cdk.aws_glue as glue
    import aws_cdk.aws_athena as athena
    import aws_cdk.aws_redshift as redshift
    import aws_cdk.aws_emr as emr

    # =========================================================================
    # A) S3 DATA LAKE — Zone architecture
    # =========================================================================
    # 4-zone data lake: Raw → Processed → Curated → Features
    # Each zone has progressively higher data quality

    lake_zones = {
        "raw": {
            "description": "Raw ingested data — never modified, source of truth",
            "retention_days": 365 * 7,  # 7 years for compliance
            "lifecycle_intelligent_tiering": 0,  # Immediately (unpredictable access)
        },
        "processed": {
            "description": "Cleaned, deduped, standardized Parquet/Iceberg data",
            "retention_days": 365 * 3,
            "lifecycle_intelligent_tiering": 30,
        },
        "curated": {
            "description": "Business-ready aggregated datasets, validated schemas",
            "retention_days": 365 * 2,
            "lifecycle_intelligent_tiering": 90,
        },
        "features": {
            "description": "ML feature store offline store output",
            "retention_days": 365,
            "lifecycle_intelligent_tiering": 30,
        },
    }

    self.lake_buckets: Dict[str, s3.Bucket] = {}

    for zone_name, config in lake_zones.items():
        bucket = s3.Bucket(
            self, f"DataLake{zone_name.capitalize()}",
            bucket_name=f"{{project_name}}-datalake-{zone_name}-{stage_name}-{self.account}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.KMS,
            encryption_key=self.kms_key,
            versioned=True,

            # EventBridge notifications (trigger Glue jobs on new data)
            event_bridge_enabled=True,

            lifecycle_rules=[
                s3.LifecycleRule(
                    id=f"{zone_name}-retention",
                    enabled=True,
                    expiration=Duration.days(config["retention_days"]),
                    transitions=[
                        s3.Transition(
                            storage_class=s3.StorageClass.INTELLIGENT_TIERING,
                            transition_after=Duration.days(config["lifecycle_intelligent_tiering"]),
                        ),
                    ] if config["lifecycle_intelligent_tiering"] > 0 else [],
                )
            ],

            removal_policy=RemovalPolicy.RETAIN if stage_name == "prod" else RemovalPolicy.DESTROY,
        )
        self.lake_buckets[zone_name] = bucket

    # =========================================================================
    # B) AWS GLUE — ETL Jobs, Data Catalog, Crawlers
    # =========================================================================

    # Glue IAM role
    glue_role = iam.Role(
        self, "GlueRole",
        assumed_by=iam.ServicePrincipal("glue.amazonaws.com"),
        role_name=f"{{project_name}}-glue-role-{stage_name}",
        managed_policies=[
            iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSGlueServiceRole"),
        ],
    )
    for bucket in self.lake_buckets.values():
        bucket.grant_read_write(glue_role)
    self.kms_key.grant_encrypt_decrypt(glue_role)

    # Glue Database (Data Catalog namespace)
    glue_db = glue.CfnDatabase(
        self, "GlueDatabase",
        catalog_id=self.account,
        database_input=glue.CfnDatabase.DatabaseInputProperty(
            name=f"{{project_name}}_{stage_name}_catalog",
            description=f"{{project_name}} data catalog for {stage_name}",
        ),
    )

    # Glue Crawler — auto-discover schema in S3 and register in catalog
    # Runs on schedule to keep the catalog up to date
    glue.CfnCrawler(
        self, "ProcessedDataCrawler",
        name=f"{{project_name}}-processed-crawler-{stage_name}",
        role=glue_role.role_arn,
        database_name=f"{{project_name}}_{stage_name}_catalog",
        description="Crawl processed S3 zone, update Glue Data Catalog schema",
        targets=glue.CfnCrawler.TargetsProperty(
            s3_targets=[
                glue.CfnCrawler.S3TargetProperty(
                    path=f"s3://{self.lake_buckets['processed'].bucket_name}/",
                    sample_size=10,
                )
            ]
        ),
        schedule=glue.CfnCrawler.ScheduleProperty(
            schedule_expression="cron(0 6 * * ? *)",  # Run daily at 6am UTC
        ) if stage_name != "dev" else None,
        configuration=json.dumps({
            "Version": 1.0,
            "CrawlerOutput": {
                "Partitions": {"AddOrUpdateBehavior": "InheritFromTable"},
                "Tables": {"AddOrUpdateBehavior": "MergeNewColumns"},
            },
            "Grouping": {
                "TableGroupingPolicy": "CombineCompatibleSchemas",
            },
        }),
    )

    # Glue ETL Job — Raw → Processed transformation
    # [Claude: generate one job per detected data transformation step from Architecture Map]
    glue.CfnJob(
        self, "RawToProcessedJob",
        name=f"{{project_name}}-raw-to-processed-{stage_name}",
        role=glue_role.role_arn,
        description="Transform raw ingested data → cleaned Parquet in processed zone",

        # Glue 4.0 = Spark 3.3, Python 3.10
        glue_version="4.0",

        command=glue.CfnJob.JobCommandProperty(
            name="glueetl",            # Spark ETL job
            script_location=f"s3://{self.lake_buckets['raw'].bucket_name}/glue-scripts/raw_to_processed.py",
            python_version="3",
        ),

        default_arguments={
            "--job-language": "python",
            "--enable-metrics": "true",
            "--enable-continuous-cloudwatch-log": "true",
            "--enable-spark-ui": "true",
            "--spark-event-logs-path": f"s3://{self.lake_buckets['raw'].bucket_name}/spark-logs/",
            "--TempDir": f"s3://{self.lake_buckets['raw'].bucket_name}/glue-temp/",
            "--source_bucket": self.lake_buckets["raw"].bucket_name,
            "--target_bucket": self.lake_buckets["processed"].bucket_name,
            "--stage": stage_name,
            "--enable-glue-datacatalog": "true",
            # Iceberg format for ACID transactions + time-travel
            "--datalake-formats": "iceberg",
            "--conf": "spark.sql.extensions=org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions",
        },

        # Worker sizing by environment
        number_of_workers=2 if stage_name == "dev" else 10,
        worker_type="G.1X" if stage_name == "dev" else "G.2X",  # G.2X = 8 vCPU, 32GB RAM per worker

        max_retries=1,
        timeout=120,  # 2 hours max per run

        # Security config: encrypt data at rest in Glue
        security_configuration=f"{{project_name}}-glue-security-{stage_name}",

        # Keep connections warm (reduces cold start for frequent jobs)
        execution_property=glue.CfnJob.ExecutionPropertyProperty(max_concurrent_runs=3),

        tags={"Project": "{{project_name}}", "Environment": stage_name, "Layer": "DataPlatform"},
    )

    # Glue Security Configuration (encrypt job bookmarks, logs, S3 output)
    glue.CfnSecurityConfiguration(
        self, "GlueSecurityConfig",
        name=f"{{project_name}}-glue-security-{stage_name}",
        encryption_configuration=glue.CfnSecurityConfiguration.EncryptionConfigurationProperty(
            s3_encryptions=[
                glue.CfnSecurityConfiguration.S3EncryptionProperty(
                    kms_key_arn=self.kms_key.key_arn,
                    s3_encryption_mode="SSE-KMS",
                )
            ],
            cloud_watch_encryption=glue.CfnSecurityConfiguration.CloudWatchEncryptionProperty(
                cloud_watch_encryption_mode="SSE-KMS",
                kms_key_arn=self.kms_key.key_arn,
            ),
            job_bookmarks_encryption=glue.CfnSecurityConfiguration.JobBookmarksEncryptionProperty(
                job_bookmarks_encryption_mode="CSE-KMS",
                kms_key_arn=self.kms_key.key_arn,
            ),
        ),
    )

    # =========================================================================
    # C) AMAZON ATHENA — Serverless SQL on S3
    # =========================================================================

    # Athena results bucket (query output)
    athena_results_bucket = s3.Bucket(
        self, "AthenaResults",
        bucket_name=f"{{project_name}}-athena-results-{stage_name}-{self.account}",
        block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
        encryption=s3.BucketEncryption.KMS,
        encryption_key=self.kms_key,
        lifecycle_rules=[
            s3.LifecycleRule(
                id="expire-query-results",
                expiration=Duration.days(30),  # Delete old query results
                enabled=True,
            )
        ],
        removal_policy=RemovalPolicy.DESTROY,
    )

    # Athena Workgroup (query cost controls + encryption settings)
    athena.CfnWorkGroup(
        self, "DataScienceWorkgroup",
        name=f"{{project_name}}-datascience-{stage_name}",
        description="Athena workgroup for data scientists and ML pipelines",
        state="ENABLED",
        work_group_configuration=athena.CfnWorkGroup.WorkGroupConfigurationProperty(
            result_configuration=athena.CfnWorkGroup.ResultConfigurationProperty(
                output_location=f"s3://{athena_results_bucket.bucket_name}/query-results/",
                encryption_configuration=athena.CfnWorkGroup.EncryptionConfigurationProperty(
                    encryption_option="SSE_KMS",
                    kms_key=self.kms_key.key_arn,
                ),
            ),
            # Cost control: alert if query exceeds 10GB scan
            bytes_scanned_cutoff_per_query=10 * 1024 * 1024 * 1024,  # 10 GB
            enforce_work_group_configuration=True,
            publish_cloud_watch_metrics_enabled=True,
            requester_pays_enabled=False,
        ),
    )

    # =========================================================================
    # D) AWS LAKE FORMATION — Data governance + column-level security
    # =========================================================================
    # [Include when SOW mentions: "data governance", "column-level security",
    #  "PII masking", "data access control", "HIPAA data lake"]

    # Lake Formation admin role
    lakeformation.CfnDataLakeSettings(
        self, "LakeFormationSettings",
        admins=[
            lakeformation.CfnDataLakeSettings.DataLakePrincipalProperty(
                data_lake_principal_identifier=glue_role.role_arn,
            )
        ],
        create_database_default_permissions=[],  # Remove default IAMAllowedPrincipals
        create_table_default_permissions=[],      # Enforce explicit Lake Formation grants
    )

    # =========================================================================
    # E) AMAZON REDSHIFT SERVERLESS — Data warehouse
    # =========================================================================
    # [Include when SOW mentions: "data warehouse", "BI", "reporting", "QuickSight", "analysts"]

    if stage_name != "dev":  # Redshift has a minimum cost, skip in dev
        redshift_namespace = redshift.CfnNamespace(
            self, "RedshiftNamespace",
            namespace_name=f"{{project_name}}-{stage_name}",
            db_name="{{project_name}}_dw",
            admin_username="admin",
            admin_user_password=self.db_secret.secret_value_from_json("password").unsafe_unwrap(),
            kms_key_id=self.kms_key.key_arn,
            log_exports=["userlog", "connectionlog", "useractivitylog"],
        )

        redshift.CfnWorkgroup(
            self, "RedshiftWorkgroup",
            workgroup_name=f"{{project_name}}-{stage_name}",
            namespace_name=redshift_namespace.namespace_name,
            base_capacity=8 if stage_name == "staging" else 32,  # RPUs (Redshift Processing Units)
            enhanced_vpc_routing=True,
            subnet_ids=[s.subnet_id for s in self.vpc.isolated_subnets],
            security_group_ids=[self.aurora_sg.security_group_id],  # Reuse isolated SG
            publicly_accessible=False,
        )

    # =========================================================================
    # F) EMR SERVERLESS — Large-scale Spark (feature engineering at scale)
    # =========================================================================
    # [Include when SOW mentions: "petabyte", "Spark", "large-scale ETL", "PySpark", ">100GB features"]

    emr_role = iam.Role(
        self, "EMRServerlessRole",
        assumed_by=iam.ServicePrincipal("emr-serverless.amazonaws.com"),
        role_name=f"{{project_name}}-emr-role-{stage_name}",
    )
    for bucket in self.lake_buckets.values():
        bucket.grant_read_write(emr_role)
    self.kms_key.grant_encrypt_decrypt(emr_role)

    emr_app = emr.CfnApplication(
        self, "EMRServerlessApp",
        name=f"{{project_name}}-spark-{stage_name}",
        type="SPARK",
        release_label="emr-6.15.0",

        # Pre-initialize workers (reduce cold start for ML pipelines)
        initial_capacity={
            "Driver": emr.CfnApplication.InitialCapacityConfigProperty(
                worker_count=1,
                worker_configuration=emr.CfnApplication.WorkerConfigurationProperty(
                    cpu="4vCPU", memory="16gb",
                ),
            ),
            "Executor": emr.CfnApplication.InitialCapacityConfigProperty(
                worker_count=4,
                worker_configuration=emr.CfnApplication.WorkerConfigurationProperty(
                    cpu="4vCPU", memory="16gb", disk="200gb",
                ),
            ),
        } if stage_name == "prod" else {},

        # Auto-stop idle applications (cost saving)
        auto_stop_configuration=emr.CfnApplication.AutoStopConfigurationProperty(
            enabled=True,
            idle_timeout_minutes=15,
        ),

        # VPC placement
        network_configuration=emr.CfnApplication.NetworkConfigurationProperty(
            subnet_ids=[s.subnet_id for s in self.vpc.private_subnets],
            security_group_ids=[self.lambda_sg.security_group_id],
        ),

        tags=[{"key": "Project", "value": "{{project_name}}"}, {"key": "Environment", "value": stage_name}],
    )

    # =========================================================================
    # OUTPUTS
    # =========================================================================
    for zone_name, bucket in self.lake_buckets.items():
        CfnOutput(self, f"DataLake{zone_name.capitalize()}Bucket",
            value=bucket.bucket_name,
            description=f"S3 data lake {zone_name} zone bucket name",
            export_name=f"{{project_name}}-datalake-{zone_name}-{stage_name}",
        )

    CfnOutput(self, "GlueDatabaseName",
        value=f"{{project_name}}_{stage_name}_catalog",
        description="Glue Data Catalog database name",
        export_name=f"{{project_name}}-glue-db-{stage_name}",
    )

    CfnOutput(self, "AthenaWorkgroupName",
        value=f"{{project_name}}-datascience-{stage_name}",
        description="Athena workgroup for data science queries",
        export_name=f"{{project_name}}-athena-workgroup-{stage_name}",
    )
```
