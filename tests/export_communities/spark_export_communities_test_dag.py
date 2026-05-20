"""
Integration test DAG for ExportConceptsTransformerJob (communities test variant).

Replaces the former Oozie-based test workflow
(eu/dnetlib/iis/wf/transformers/export/communities/test)
with an Airflow DAG that can be run in a Kubernetes environment.

Note: ExportConceptsTransformerJob has no report output. Only the data output
is validated in this test.

Three-step test:
  1. prepare_input  – converts JSON fixtures into Avro data stores on HDFS.
  2. run_job        – executes ExportConceptsTransformerJob.
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

_DATA_ROOT = "eu/dnetlib/iis/wf/transformers/export/communities/test/data"
_INPUT_JSON  = f"{_DATA_ROOT}/document_to_community_id.json"
_OUTPUT_JSON = f"{_DATA_ROOT}/document_to_concept_ids.json"

# Input uses common schemas DocumentToConceptId (not researchinitiative)
_SCHEMA_DOCUMENT_TO_CONCEPT_ID  = "eu.dnetlib.iis.referenceextraction.common.schemas.DocumentToConceptId"
_SCHEMA_DOCUMENT_TO_CONCEPT_IDS = "eu.dnetlib.iis.export.schemas.DocumentToConceptIds"

EXECUTION_TIMEOUT = int(os.getenv("EXECUTION_TIMEOUT", 6))

default_args = {
    "execution_timeout": timedelta(days=EXECUTION_TIMEOUT),
    "retries": int(os.getenv("DEFAULT_TASK_RETRIES", 1)),
    "retry_delay": timedelta(seconds=int(os.getenv("DEFAULT_RETRY_DELAY_SECONDS", 60))),
}


@dag(
    dag_id="spark_export_communities_test",
    dag_display_name="Integration test for ExportConceptsTransformerJob (communities variant)",
    default_args=default_args,
    params={
        "JAR": Param(
            default="https://maven.ceon.pl/artifactory/iis-snapshots/eu/dnetlib/iis/iis-wf-transformers/1.3.0-SNAPSHOT/iis-wf-transformers-1.3.0-20260520.160946-5-test-uber.jar",
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
            default="hdfs://iis-cdh5-test-m1.ocean.icm.edu.pl:8020/tmp/marek.horst/export-communities-test",
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
def spark_export_communities_test():
    """Three-step integration test for ExportConceptsTransformerJob (communities variant)."""

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
            "-schemaClass",   _SCHEMA_DOCUMENT_TO_CONCEPT_ID,
            "-classpathJson", _INPUT_JSON,
            "-hdfsOutput",    "{{ params.get('workingDir') }}/producer/output",
        ],
        spark_extra_conf={
            "spark.driver.memory":      "{{ params.get('sparkDriverMemory') }}",
            "spark.executor.instances": "0",
            **hadoop_user_conf,
        },
    )

    run_job = generate_spark_operator(
        task_id="run_job",
        task_display_name="Run ExportConceptsTransformerJob (job under test)",
        jar="{{ params.get('JAR') }}",
        image="{{ params.get('SPARK_IMAGE') }}",
        main_class="eu.dnetlib.iis.wf.transformers.export.concepts.ExportConceptsTransformerJob",
        arguments=[
            "-input",  "{{ params.get('workingDir') }}/producer/output",
            "-output", "{{ params.get('workingDir') }}/transformer_export_concepts/output",
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
        task_display_name="Validate export concepts output against expected JSON (SparkAvroTestConsumer)",
        jar="{{ params.get('JAR') }}",
        image="{{ params.get('SPARK_IMAGE') }}",
        main_class="eu.dnetlib.iis.common.spark.SparkAvroTestConsumer",
        arguments=[
            "-schemaClass",   _SCHEMA_DOCUMENT_TO_CONCEPT_IDS,
            "-classpathJson", _OUTPUT_JSON,
            "-hdfsInput",     "{{ params.get('workingDir') }}/transformer_export_concepts/output",
        ],
        spark_extra_conf={
            "spark.driver.memory":      "{{ params.get('sparkDriverMemory') }}",
            "spark.executor.instances": "0",
            **hadoop_user_conf,
        },
    )

    prepare_input >> run_job >> validate_output


spark_export_communities_test()
