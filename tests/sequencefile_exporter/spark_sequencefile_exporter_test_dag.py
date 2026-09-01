"""
Integration test DAG for SequenceFileExporterJob.

Replaces the former Oozie-based test workflow
(eu/dnetlib/iis/wf/export/actionmanager/sequencefile/sampledataproducer)
with an Airflow DAG that can be run in a Kubernetes environment.

Three-step test (document → referenced-projects export as the representative case):
  1. prepare_input  – converts the existing JSON fixture into an Avro datastore on HDFS.
  2. run_job        – runs SequenceFileExporterJob with DocumentToProjectActionBuilderModuleFactory.
  3. validate_output – uses SparkSequenceFileTestConsumer to assert the expected record count.

One DocumentToProject record produces two bidirectional AtomicAction<Relation> objects
(isProducedBy + produces), so the expected count is 2.
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

# Classpath path to the JSON input fixture (reuses the existing Oozie test data).
_INPUT_JSON = (
    "eu/dnetlib/iis/wf/export/actionmanager/sequencefile/sampledataproducer"
    "/input/document_to_project.json"
)

_SCHEMA_DOCUMENT_TO_PROJECT = "eu.dnetlib.iis.referenceextraction.project.schemas.DocumentToProject"
_FACTORY_DOCUMENT_TO_PROJECT = (
    "eu.dnetlib.iis.wf.export.actionmanager.module.DocumentToProjectActionBuilderModuleFactory"
)
_CONSUMER_CLASS = (
    "eu.dnetlib.iis.wf.export.actionmanager.sequencefile.SparkSequenceFileTestConsumer"
)

# DocumentToProjectActionBuilderModuleFactory produces two AtomicAction<Relation> objects
# per input record (isProducedBy and produces), so one input record → 2 output actions.
_EXPECTED_ACTION_COUNT = 2

EXECUTION_TIMEOUT = int(os.getenv("EXECUTION_TIMEOUT", 6))

default_args = {
    "execution_timeout": timedelta(days=EXECUTION_TIMEOUT),
    "retries": int(os.getenv("DEFAULT_TASK_RETRIES", 1)),
    "retry_delay": timedelta(seconds=int(os.getenv("DEFAULT_RETRY_DELAY_SECONDS", 60))),
}


@dag(
    dag_id="spark_sequencefile_exporter_test",
    dag_display_name="Integration test for SequenceFileExporterJob (document → referenced projects)",
    default_args=default_args,
    params={
        "JAR": Param(
            default="https://maven.ceon.pl/artifactory/iis-snapshots/eu/dnetlib/iis/iis-wf-export-actionmanager/1.3.0-SNAPSHOT/iis-wf-export-actionmanager-1.3.0-20260901.151211-9-test-uber.jar",
            type="string",
            description="iis-wf-export-actionmanager test uber JAR (built with -Pshade-test-uber-jar).",
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
            default="hdfs://iis-cdh5-test-m2.ocean.icm.edu.pl:8020/tmp/marek.horst/sequencefile-exporter-test",
            type="string",
            description="HDFS working directory for intermediate and output data",
        ),
        "collectedfrom_key": Param(
            default="repo-id-1",
            type="string",
            description="Datasource identifier forwarded to the ActionBuilderFactory "
                        "(export.relation.collectedfrom.key)",
        ),
        "sparkDriverMemory": Param(default="2g", type="string"),
        "sparkExecutorMemory": Param(default="2g", type="string"),
        "sparkExecutorOverhead": Param(default="512", type="string"),
    },
    tags=["openaire", "iis", "export", "actionmanager", "test"],
    schedule=None,
)
def spark_sequencefile_exporter_test():
    """Three-step integration test for SequenceFileExporterJob."""

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
        task_display_name="Prepare Avro input from JSON fixture (SparkAvroTestProducer)",
        jar="{{ params.get('JAR') }}",
        image="{{ params.get('SPARK_IMAGE') }}",
        main_class="eu.dnetlib.iis.common.spark.SparkAvroTestProducer",
        arguments=[
            "-schemaClass",   _SCHEMA_DOCUMENT_TO_PROJECT,
            "-classpathJson", _INPUT_JSON,
            "-hdfsOutput",    "{{ params.get('workingDir') }}/producer/document_to_project",
        ],
        spark_extra_conf={
            "spark.driver.memory":      "{{ params.get('sparkDriverMemory') }}",
            "spark.executor.instances": "0",
            **hadoop_user_conf,
        },
    )

    run_job = generate_spark_operator(
        task_id="run_job",
        task_display_name="Run SequenceFileExporterJob (job under test)",
        jar="{{ params.get('JAR') }}",
        image="{{ params.get('SPARK_IMAGE') }}",
        main_class="eu.dnetlib.iis.wf.export.actionmanager.sequencefile.SequenceFileExporterJob",
        arguments=[
            "-inputPath",
            "{{ params.get('workingDir') }}/producer/document_to_project",
            "-outputPath",
            "{{ params.get('workingDir') }}/output/document_referencedProjects/actionset-id",
            "-actionBuilderFactoryClassName", _FACTORY_DOCUMENT_TO_PROJECT,
            "-inputAvroSchemaClass",          _SCHEMA_DOCUMENT_TO_PROJECT,
            "-Dexport.relation.collectedfrom.key={{ params.get('collectedfrom_key') }}",
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
        task_display_name=(
            f"Validate SequenceFile output: assert exactly {_EXPECTED_ACTION_COUNT} "
            "AtomicAction records (SparkSequenceFileTestConsumer)"
        ),
        jar="{{ params.get('JAR') }}",
        image="{{ params.get('SPARK_IMAGE') }}",
        main_class=_CONSUMER_CLASS,
        arguments=[
            "-hdfsInput",
            "{{ params.get('workingDir') }}/output/document_referencedProjects/actionset-id",
            "-expectedCount", str(_EXPECTED_ACTION_COUNT),
        ],
        spark_extra_conf={
            "spark.driver.memory":      "{{ params.get('sparkDriverMemory') }}",
            "spark.executor.instances": "0",
            **hadoop_user_conf,
        },
    )

    prepare_input >> run_job >> validate_output


spark_sequencefile_exporter_test()
