"""
Integration test DAG for MetadataMergerJob.

Replaces the former Oozie-based test workflow
(eu/dnetlib/iis/wf/transformers/metadatamerger/sampledataproducer) with an
Airflow DAG that can be run in a Kubernetes environment.

The test follows the same three-step principle as the original Oozie workflow:

  1. prepare_input  – converts JSON fixtures from the classpath into Avro data
                      stores on HDFS using SparkAvroTestProducer (driver-only
                      Spark job from iis-common; HDFS access via JavaSparkContext).
  2. run_merger     – executes MetadataMergerJob (the Spark job under test).
  3. validate_output – reads the actual Avro output via SparkAvroTestConsumer
                       (driver-only Spark job from iis-common) and compares it
                       order-independently against the expected JSON fixture.

SparkAvroTestProducer and SparkAvroTestConsumer are generic: schema classes are
provided at runtime as -schemaClass arguments and resolved via reflection, so the
same two drivers can be reused across all future test DAGs.

All JSON fixtures and the generic drivers (from iis-common:test-jar) are bundled
inside the iis-wf-transformers test uber JAR (built with -Pshade-test-uber-jar).
"""

import os
import sys
from datetime import timedelta

from airflow.sdk import dag, Param

package_dir = os.path.dirname(os.path.abspath(__file__))
# Navigate up two levels to reach the iis-workflows root where the shared
# utilities (dag_utils, spark_configurator) live.
repo_root = os.path.dirname(os.path.dirname(package_dir))

for path in (package_dir, repo_root):
    if path not in sys.path:
        sys.path.append(path)

from spark_configurator import generate_spark_operator

# ---------------------------------------------------------------------------
# Classpath paths to the JSON fixtures bundled inside the uber JAR.
# These correspond to files under:
#   iis-wf-transformers/src/test/resources/
#     eu/dnetlib/iis/wf/transformers/metadatamerger/sampledataproducer/data/
# ---------------------------------------------------------------------------
_DATA_ROOT = "eu/dnetlib/iis/wf/transformers/metadatamerger/sampledataproducer/data"
_BASE_METADATA_JSON   = f"{_DATA_ROOT}/base_metadata.json"
_EXTR_METADATA_JSON   = f"{_DATA_ROOT}/extr_metadata.json"
_MERGED_METADATA_JSON = f"{_DATA_ROOT}/merged_metadata.json"

# Avro schema fully-qualified class names (resolved at runtime via reflection by
# SparkAvroTestProducer / SparkAvroTestConsumer)
_SCHEMA_DOCUMENT_METADATA  = "eu.dnetlib.iis.importer.schemas.DocumentMetadata"
_SCHEMA_EXTRACTED_METADATA = "eu.dnetlib.iis.metadataextraction.schemas.ExtractedDocumentMetadata"
_SCHEMA_MERGED_METADATA    = "eu.dnetlib.iis.transformers.metadatamerger.schemas.ExtractedDocumentMetadataMergedWithOriginal"

EXECUTION_TIMEOUT = int(os.getenv("EXECUTION_TIMEOUT", 6))

default_args = {
    "execution_timeout": timedelta(days=EXECUTION_TIMEOUT),
    "retries": int(os.getenv("DEFAULT_TASK_RETRIES", 1)),
    "retry_delay": timedelta(seconds=int(os.getenv("DEFAULT_RETRY_DELAY_SECONDS", 60))),
}


@dag(
    dag_id="spark_metadatamerger_test",
    dag_display_name="Integration test for MetadataMergerJob",
    default_args=default_args,
    params={
        "JAR": Param(
            default="https://maven.ceon.pl/artifactory/iis-snapshots/eu/dnetlib/iis/iis-wf-transformers/1.3.0-SNAPSHOT/iis-wf-transformers-1.3.0-20260514.134224-1-test-uber.jar",
            type="string",
            description="iis-wf-transformers test uber JAR (built with -Pshade-test-uber-jar). "
                        "Contains production classes + TestingConsumer/TestsIOUtils from iis-common + JSON fixtures.",
        ),
        "SPARK_IMAGE": Param(
            "docker-registry.openaire.eu/kubernetes_devel/spark:4.1.1",
            type="string",
            description="Spark Docker image",
        ),
        "HADOOP_USER_NAME": Param(
            "marek.horst",
            type="string",
            description="Hadoop user name used when writing to / reading from HDFS",
        ),
        "workingDir": Param(
            default="hdfs://nameservice1/tmp/marek.horst/metadatamerger-test",
            type="string",
            description="HDFS working directory for intermediate and output data",
        ),
        # --- Spark resource tuning ---
        "sparkDriverMemory": Param(
            default="1g",
            type="string",
            description="Memory for the Spark driver",
        ),
        "sparkExecutorMemory": Param(
            default="2g",
            type="string",
            description="Memory per Spark executor",
        ),
        "sparkExecutorOverhead": Param(
            default="512",
            type="string",
            description="Off-heap memory overhead per executor in MB",
        ),
    },
    tags=["openaire", "iis", "transformers", "test"],
    schedule=None,
)
def spark_metadatamerger_test():
    """Three-step integration test for MetadataMergerJob."""

    # ------------------------------------------------------------------
    # Shared Spark environment variables injected into driver and executor
    # pods so that HDFS operations use the correct user identity.
    # ------------------------------------------------------------------
    hadoop_user_conf = {
        "spark.driverEnv.HADOOP_USER_NAME":           "{{ params.get('HADOOP_USER_NAME') }}",
        "spark.executorEnv.HADOOP_USER_NAME":         "{{ params.get('HADOOP_USER_NAME') }}",
        "spark.driverEnv.SPARK_USER":                 "{{ params.get('HADOOP_USER_NAME') }}",
        "spark.executorEnv.SPARK_USER":               "{{ params.get('HADOOP_USER_NAME') }}",
        "spark.kubernetes.driverEnv.HADOOP_USER_NAME": "{{ params.get('HADOOP_USER_NAME') }}",
        "spark.kubernetes.executorEnv.HADOOP_USER_NAME": "{{ params.get('HADOOP_USER_NAME') }}",
    }

    # ------------------------------------------------------------------
    # Step 1 – prepare_input
    #
    # Runs SparkAvroTestProducer (generic, from iis-common) as a
    # driver-only Spark job.  Schema classes are resolved at runtime via
    # reflection; no module-specific producer code needed.
    # ------------------------------------------------------------------
    prepare_input = generate_spark_operator(
        task_id="prepare_input",
        task_display_name="Prepare Avro input from JSON fixtures (SparkAvroTestProducer)",
        jar="{{ params.get('JAR') }}",
        image="{{ params.get('SPARK_IMAGE') }}",
        main_class="eu.dnetlib.iis.common.spark.SparkAvroTestProducer",
        arguments=[
            "-schemaClass",   _SCHEMA_DOCUMENT_METADATA,
            "-classpathJson", _BASE_METADATA_JSON,
            "-hdfsOutput",    "{{ params.get('workingDir') }}/producer/base_metadata",
            "-schemaClass",   _SCHEMA_EXTRACTED_METADATA,
            "-classpathJson", _EXTR_METADATA_JSON,
            "-hdfsOutput",    "{{ params.get('workingDir') }}/producer/extr_metadata",
        ],
        spark_extra_conf={
            "spark.driver.memory": "{{ params.get('sparkDriverMemory') }}",
            # Driver-only job – no executors needed.
            "spark.executor.instances": "0",
            **hadoop_user_conf,
        },
    )

    # ------------------------------------------------------------------
    # Step 2 – run_merger
    #
    # Executes MetadataMergerJob: the actual Spark job under test.
    # ------------------------------------------------------------------
    run_merger = generate_spark_operator(
        task_id="run_merger",
        task_display_name="Run MetadataMergerJob (job under test)",
        jar="{{ params.get('JAR') }}",
        image="{{ params.get('SPARK_IMAGE') }}",
        main_class="eu.dnetlib.iis.wf.transformers.metadatamerger.MetadataMergerJob",
        arguments=[
            "-inputBaseMetadata",       "{{ params.get('workingDir') }}/producer/base_metadata",
            "-inputExtractedMetadata",  "{{ params.get('workingDir') }}/producer/extr_metadata",
            "-outputMergedMetadata",    "{{ params.get('workingDir') }}/merger/merged_metadata",
        ],
        spark_extra_conf={
            "spark.driver.memory":          "{{ params.get('sparkDriverMemory') }}",
            "spark.executor.memory":        "{{ params.get('sparkExecutorMemory') }}",
            "spark.executor.memoryOverhead": "{{ params.get('sparkExecutorOverhead') }}",
            **hadoop_user_conf,
        },
    )

    # ------------------------------------------------------------------
    # Step 3 – validate_output
    #
    # Runs SparkAvroTestConsumer (generic, from iis-common) as a
    # driver-only Spark job.  Reads the actual Avro output from HDFS and
    # compares it order-independently with the expected JSON fixture.
    # A test failure causes a non-zero Spark driver exit.
    # ------------------------------------------------------------------
    validate_output = generate_spark_operator(
        task_id="validate_output",
        task_display_name="Validate merger output against expected JSON (SparkAvroTestConsumer)",
        jar="{{ params.get('JAR') }}",
        image="{{ params.get('SPARK_IMAGE') }}",
        main_class="eu.dnetlib.iis.common.spark.SparkAvroTestConsumer",
        arguments=[
            "-schemaClass",   _SCHEMA_MERGED_METADATA,
            "-classpathJson", _MERGED_METADATA_JSON,
            "-hdfsInput",     "{{ params.get('workingDir') }}/merger/merged_metadata",
        ],
        spark_extra_conf={
            "spark.driver.memory": "{{ params.get('sparkDriverMemory') }}",
            # Driver-only job – no executors needed.
            "spark.executor.instances": "0",
            **hadoop_user_conf,
        },
    )

    prepare_input >> run_merger >> validate_output


spark_metadatamerger_test()
