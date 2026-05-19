"""
Integration test DAG for ChecksumMetaPostprocessingJob.

Replaces the former Oozie-based test workflow
(eu/dnetlib/iis/wf/transformers/metadataextraction/checksum/postprocessing/meta/sampledataproducer)
with an Airflow DAG that can be run in a Kubernetes environment.

Three-step test:
  1. prepare_input  – converts JSON fixtures into Avro data stores on HDFS.
  2. run_job        – executes ChecksumMetaPostprocessingJob.
  3. validate_output – compares Avro output against expected JSON.
"""

import os
import sys
from datetime import timedelta

from airflow.sdk import dag, Param

package_dir = os.path.dirname(os.path.abspath(__file__))
repo_root = os.path.dirname(os.path.dirname(package_dir))

for path in (package_dir, repo_root):
    if path not in sys.path:
        sys.path.append(path)

from spark_configurator import generate_spark_operator

_DATA_ROOT = "eu/dnetlib/iis/wf/transformers/metadataextraction/checksum/postprocessing/meta/sampledataproducer/data"
_INPUT_DOCUMENT_CONTENT_URL_JSON        = f"{_DATA_ROOT}/input_document_content_url.json"
_INPUT_EXTRACTED_DOCUMENT_METADATA_JSON = f"{_DATA_ROOT}/input_extracted_document_metadata.json"
_OUTPUT_EXTRACTED_DOCUMENT_METADATA_JSON = f"{_DATA_ROOT}/output_extracted_document_metadata.json"

_SCHEMA_DOCUMENT_CONTENT_URL = "eu.dnetlib.iis.importer.auxiliary.schemas.DocumentContentUrl"
_SCHEMA_EXTRACTED_METADATA   = "eu.dnetlib.iis.metadataextraction.schemas.ExtractedDocumentMetadata"

EXECUTION_TIMEOUT = int(os.getenv("EXECUTION_TIMEOUT", 6))

default_args = {
    "execution_timeout": timedelta(days=EXECUTION_TIMEOUT),
    "retries": int(os.getenv("DEFAULT_TASK_RETRIES", 1)),
    "retry_delay": timedelta(seconds=int(os.getenv("DEFAULT_RETRY_DELAY_SECONDS", 60))),
}


@dag(
    dag_id="spark_checksum_meta_postprocessing_test",
    dag_display_name="Integration test for ChecksumMetaPostprocessingJob",
    default_args=default_args,
    params={
        "JAR": Param(
            default="https://maven.ceon.pl/artifactory/iis-snapshots/eu/dnetlib/iis/iis-wf-transformers/1.3.0-SNAPSHOT/iis-wf-transformers-1.3.0-20260519.155423-2-test-uber.jar",
            type="string",
            description="iis-wf-transformers test uber JAR (built with -Pshade-test-uber-jar).",
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
            default="hdfs://iis-cdh5-test-m1.ocean.icm.edu.pl:8020/tmp/marek.horst/checksum-meta-postprocessing-test",
            type="string",
            description="HDFS working directory for intermediate and output data",
        ),
        "sparkDriverMemory": Param(default="1g", type="string"),
        "sparkExecutorMemory": Param(default="2g", type="string"),
        "sparkExecutorOverhead": Param(default="512", type="string"),
    },
    tags=["openaire", "iis", "transformers", "test"],
    schedule=None,
)
def spark_checksum_meta_postprocessing_test():
    """Three-step integration test for ChecksumMetaPostprocessingJob."""

    hadoop_user_conf = {
        "spark.driverEnv.HADOOP_USER_NAME":              "{{ params.get('HADOOP_USER_NAME') }}",
        "spark.executorEnv.HADOOP_USER_NAME":            "{{ params.get('HADOOP_USER_NAME') }}",
        "spark.driverEnv.SPARK_USER":                    "{{ params.get('HADOOP_USER_NAME') }}",
        "spark.executorEnv.SPARK_USER":                  "{{ params.get('HADOOP_USER_NAME') }}",
        "spark.kubernetes.driverEnv.HADOOP_USER_NAME":   "{{ params.get('HADOOP_USER_NAME') }}",
        "spark.kubernetes.executorEnv.HADOOP_USER_NAME": "{{ params.get('HADOOP_USER_NAME') }}",
    }

    prepare_input = generate_spark_operator(
        task_id="prepare_input",
        task_display_name="Prepare Avro input from JSON fixtures (SparkAvroTestProducer)",
        jar="{{ params.get('JAR') }}",
        image="{{ params.get('SPARK_IMAGE') }}",
        main_class="eu.dnetlib.iis.common.spark.SparkAvroTestProducer",
        arguments=[
            "-schemaClass",   _SCHEMA_DOCUMENT_CONTENT_URL,
            "-classpathJson", _INPUT_DOCUMENT_CONTENT_URL_JSON,
            "-hdfsOutput",    "{{ params.get('workingDir') }}/producer/document_content_url",
            "-schemaClass",   _SCHEMA_EXTRACTED_METADATA,
            "-classpathJson", _INPUT_EXTRACTED_DOCUMENT_METADATA_JSON,
            "-hdfsOutput",    "{{ params.get('workingDir') }}/producer/extracted_document_metadata",
        ],
        spark_extra_conf={
            "spark.driver.memory":      "{{ params.get('sparkDriverMemory') }}",
            "spark.executor.instances": "0",
            **hadoop_user_conf,
        },
    )

    run_job = generate_spark_operator(
        task_id="run_job",
        task_display_name="Run ChecksumMetaPostprocessingJob (job under test)",
        jar="{{ params.get('JAR') }}",
        image="{{ params.get('SPARK_IMAGE') }}",
        main_class="eu.dnetlib.iis.wf.transformers.metadataextraction.checksum.postprocessing.meta.ChecksumMetaPostprocessingJob",
        arguments=[
            "-inputExtractedDocumentMetadata", "{{ params.get('workingDir') }}/producer/extracted_document_metadata",
            "-inputDocumentContentUrl",        "{{ params.get('workingDir') }}/producer/document_content_url",
            "-output",                         "{{ params.get('workingDir') }}/transformer_metadataextraction_checksum_postprocessing_meta/output",
        ],
        spark_extra_conf={
            "spark.driver.memory":           "{{ params.get('sparkDriverMemory') }}",
            "spark.executor.memory":         "{{ params.get('sparkExecutorMemory') }}",
            "spark.executor.memoryOverhead": "{{ params.get('sparkExecutorOverhead') }}",
            **hadoop_user_conf,
        },
    )

    validate_output = generate_spark_operator(
        task_id="validate_output",
        task_display_name="Validate metadata output against expected JSON (SparkAvroTestConsumer)",
        jar="{{ params.get('JAR') }}",
        image="{{ params.get('SPARK_IMAGE') }}",
        main_class="eu.dnetlib.iis.common.spark.SparkAvroTestConsumer",
        arguments=[
            "-schemaClass",   _SCHEMA_EXTRACTED_METADATA,
            "-classpathJson", _OUTPUT_EXTRACTED_DOCUMENT_METADATA_JSON,
            "-hdfsInput",     "{{ params.get('workingDir') }}/transformer_metadataextraction_checksum_postprocessing_meta/output",
        ],
        spark_extra_conf={
            "spark.driver.memory":      "{{ params.get('sparkDriverMemory') }}",
            "spark.executor.instances": "0",
            **hadoop_user_conf,
        },
    )

    prepare_input >> run_job >> validate_output


spark_checksum_meta_postprocessing_test()
