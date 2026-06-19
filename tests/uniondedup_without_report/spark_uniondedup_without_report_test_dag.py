"""
Integration test DAG for UnionDedupJob (without_report test variant).

Replaces the former Oozie-based test workflow
(eu/dnetlib/iis/wf/transformers/common/uniondedup/without_report)
with an Airflow DAG that can be run in a Kubernetes environment.

Three-step test:
  1. prepare_input  – converts JSON fixtures into Avro data stores on HDFS.
  2. run_job        – executes UnionDedupJob.
  3. validate_output – compares Avro deduplicated union output against expected JSON.
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

_INPUT_ROOT  = "eu/dnetlib/iis/wf/transformers/common/uniondedup/input"
_OUTPUT_ROOT = "eu/dnetlib/iis/wf/transformers/common/uniondedup/expected_output"
_DATASTORE_A_JSON    = f"{_INPUT_ROOT}/datastore_a.json"
_DATASTORE_B_JSON    = f"{_INPUT_ROOT}/datastore_b.json"
_UNION_DATASTORE_JSON = f"{_OUTPUT_ROOT}/union_datastore.json"

_SCHEMA_DOCUMENT_TO_PROJECT = "eu.dnetlib.iis.referenceextraction.project.schemas.DocumentToProject"

EXECUTION_TIMEOUT = int(os.getenv("EXECUTION_TIMEOUT", 6))

default_args = {
    "execution_timeout": timedelta(days=EXECUTION_TIMEOUT),
    "retries": int(os.getenv("DEFAULT_TASK_RETRIES", 1)),
    "retry_delay": timedelta(seconds=int(os.getenv("DEFAULT_RETRY_DELAY_SECONDS", 60))),
}


@dag(
    dag_id="spark_uniondedup_without_report_test",
    dag_display_name="Integration test for UnionDedupJob (without_report variant)",
    default_args=default_args,
    params={
        "JAR": Param(
            default="https://maven.ceon.pl/artifactory/iis-snapshots/eu/dnetlib/iis/iis-wf-transformers/1.3.0-SNAPSHOT/iis-wf-transformers-1.3.0-20260520.160946-5-test-uber.jar",
            type="string",
            description="iis-wf-transformers test uber JAR (built with -Pshade-test-uber-jar).",
        ),
        "SPARK_IMAGE": Param(
            "docker-registry.openaire.eu/kubernetes_devel/spark:4.1.2",
            type="string",
            description="Spark Docker image",
        ),
        "HADOOP_USER_NAME": Param(
            "marek.horst",
            type="string",
            description="Hadoop user name used when writing to / reading from HDFS",
        ),
        "workingDir": Param(
            default="hdfs://iis-cdh5-test-m1.ocean.icm.edu.pl:8020/tmp/marek.horst/uniondedup-without-report-test",
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
def spark_uniondedup_without_report_test():
    """Three-step integration test for UnionDedupJob (without_report variant)."""

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
            "-schemaClass",   _SCHEMA_DOCUMENT_TO_PROJECT,
            "-classpathJson", _DATASTORE_A_JSON,
            "-hdfsOutput",    "{{ params.get('workingDir') }}/producer/datastore_a",
            "-schemaClass",   _SCHEMA_DOCUMENT_TO_PROJECT,
            "-classpathJson", _DATASTORE_B_JSON,
            "-hdfsOutput",    "{{ params.get('workingDir') }}/producer/datastore_b",
        ],
        spark_extra_conf={
            "spark.driver.memory":      "{{ params.get('sparkDriverMemory') }}",
            "spark.executor.instances": "0",
            **hadoop_user_conf,
        },
    )

    run_job = generate_spark_operator(
        task_id="run_job",
        task_display_name="Run UnionDedupJob (job under test)",
        jar="{{ params.get('JAR') }}",
        image="{{ params.get('SPARK_IMAGE') }}",
        main_class="eu.dnetlib.iis.wf.transformers.common.uniondedup.UnionDedupJob",
        arguments=[
            "-inputA",        "{{ params.get('workingDir') }}/producer/datastore_a",
            "-inputB",        "{{ params.get('workingDir') }}/producer/datastore_b",
            "-output",        "{{ params.get('workingDir') }}/uniondedup",
            "-schemaClass",   _SCHEMA_DOCUMENT_TO_PROJECT,
            "-groupByField1", "documentId",
            "-groupByField2", "projectId",
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
        task_display_name="Validate dedup union output against expected JSON (SparkAvroTestConsumer)",
        jar="{{ params.get('JAR') }}",
        image="{{ params.get('SPARK_IMAGE') }}",
        main_class="eu.dnetlib.iis.common.spark.SparkAvroTestConsumer",
        arguments=[
            "-schemaClass",   _SCHEMA_DOCUMENT_TO_PROJECT,
            "-classpathJson", _UNION_DATASTORE_JSON,
            "-hdfsInput",     "{{ params.get('workingDir') }}/uniondedup",
        ],
        spark_extra_conf={
            "spark.driver.memory":      "{{ params.get('sparkDriverMemory') }}",
            "spark.executor.instances": "0",
            **hadoop_user_conf,
        },
    )

    prepare_input >> run_job >> validate_output


spark_uniondedup_without_report_test()
