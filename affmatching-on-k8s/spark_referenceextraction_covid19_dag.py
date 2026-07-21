import os
import sys
from datetime import timedelta

from airflow.decorators import dag
from airflow.hooks.base import BaseHook
from airflow.models.param import Param
from airflow.providers.standard.operators.bash import BashOperator

package_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(package_dir)

# Ensure it's in sys.path
if package_dir not in sys.path:
    sys.path.append(package_dir)
# This is important to reference common set of utility methods stored in the main workflow project repository folder
if parent_dir not in sys.path:
    sys.path.append(parent_dir)

import dag_utils
from spark_configurator import generate_spark_operator, java_action

EXECUTION_TIMEOUT = int(os.getenv("EXECUTION_TIMEOUT", 6))

default_args = {
    "execution_timeout": timedelta(days=EXECUTION_TIMEOUT),
    "retries": int(os.getenv("DEFAULT_TASK_RETRIES", 1)),
    "retry_delay": timedelta(seconds=int(os.getenv("DEFAULT_RETRY_DELAY_SECONDS", 60)))
}

@dag(
    dag_id="spark_referenceextraction_covid19",
    dag_display_name="Extracts COVID-19 references from document metadata using MADIS/SQLite matching",
    default_args=default_args,
    params={
        "JAR": Param(
            default="https://maven.ceon.pl/artifactory/iis-snapshots/eu/dnetlib/iis/iis-wf-referenceextraction/1.3.0-SNAPSHOT/iis-wf-referenceextraction-1.3.0-20260721.094043-3-uber.jar",
            type='string',
            description="iis-wf-referenceextraction uber jar"
        ),
        # madis-with-spark extends the base spark image with Python 2.7, apsw, and
        # the Madis query application (MADIS_HOME=/opt/madis).  This is required
        # because Covid19ReferenceExtractionJob uses RDD.pipe() to run madis SQL.
        "SPARK_IMAGE": Param(
            "docker-registry.openaire.eu/kubernetes_devel/madis-with-spark:4.1.2",
            type='string',
            description="Spark Docker image; must include Python 2.7 + Madis (MADIS_HOME set)"),
        "HADOOP_USER_NAME": Param(
            "marek.horst",
            type='string',
            description=""),

        # --- I/O paths ---

        # --- currently this path is set explicitly, no extraction from jar package due to the missing `hdfs` command on the pod
        "scriptDirPath": Param(
            default="hdfs://nameservice1/tmp/marek.horst/referenceextraction_covid19/scripts",
            type="string",
            description="Input HDFS path holding covid-19 madis script",
        ),

        "inputAvroPath": Param(
            default="hdfs://nameservice1/user/dnet.production/iis/working_dirs/primary/transformers_metadatamerger/output_merged_metadata",
            type="string",
            description="Input Avro path with document metadata "
                        "(eu.dnetlib.iis.transformers.metadatamerger.schemas.ExtractedDocumentMetadataMergedWithOriginal)",
        ),
        "outputAvroPath": Param(
            default="hdfs://iis-cdh5-test-m1.ocean.icm.edu.pl:8020/tmp/marek.horst/referenceextraction_covid19/output",
            type="string",
            description="Output Avro path for matched documents "
                        "(eu.dnetlib.iis.referenceextraction.covid19.schemas.MatchedDocument)",
        ),
        "outputReportPath": Param(
            default="hdfs://iis-cdh5-test-m1.ocean.icm.edu.pl:8020/tmp/marek.horst/referenceextraction_covid19/reports",
            type="string",
            description="Output Avro path for execution reports (eu.dnetlib.iis.common.schemas.ReportEntry)",
        ),

        # --- Algorithm parameters ---
        "predefinedConceptId": Param(
            default="covid-19",
            type="string",
            description="Concept identifier assigned to every matched document",
        ),
        "predefinedConfidenceLevel": Param(
            default="0.8",
            type="string",
            description="Confidence level assigned to every matched document",
        ),

        # --- Spark tuning ---
        "numberOfPartitions": Param(
            default="",
            type="string",
            description="Number of RDD partitions for the input dataset "
                        "(leave empty to use the natural partition count of the input Avro files)",
        ),
        "sparkDriverMemory": Param(
            default="8g",
            type="string",
            description="Memory for the Spark driver",
        ),
        "sparkExecutorMemory": Param(
            default="8g",
            type="string",
            description="Memory per Spark executor",
        ),
    },
    tags=["openaire", "iis", "referenceextraction", "covid19"],
    schedule=None
)
def referenceextraction_covid19():
    # ---------------------------------------------------------------------------
    # NOTICE: currently this step is disabled because of the missing `hdfs` command on the pod.
    # ---------------------------------------------------------------------------
    # 
    # Step 1: extract the matching scripts from the uber-JAR and stage them on
    # HDFS.  The JAR is a ZIP archive, so unzip can pull out just the scripts/
    # subtree without downloading twice.  The extracted scripts/ directory is
    # placed under a path derived from the JAR filename so each snapshot lands
    # in its own directory and re-runs are idempotent.
    #
    # The HDFS path is pushed as XCom (last stdout line) and consumed by the
    # Spark task below via ti.xcom_pull().
    # ---------------------------------------------------------------------------
    if False:  # TODO: re-enable once `hdfs` is available on the Airflow worker; see extract_scripts >> spark_task below
        extract_scripts = BashOperator(
            task_id="extract_scripts_from_jar",
            task_display_name="Extract COVID-19 matching scripts from uber-JAR to HDFS",
            bash_command="""
set -euo pipefail

export HADOOP_USER_NAME="{{ params.get('HADOOP_USER_NAME') }}"

JAR_URL="{{ params.get('JAR') }}"
JAR_FILENAME=$(basename "$JAR_URL")

# Versioned HDFS parent derived from the JAR snapshot filename so every
# distinct build lands in its own directory and re-runs are safe.
SCRIPTS_HDFS_PARENT="hdfs://iis-cdh5-test-m1.ocean.icm.edu.pl:8020/tmp/airflow/iis/covid19-scripts/${JAR_FILENAME%.jar}"
SCRIPTS_HDFS_PATH="${SCRIPTS_HDFS_PARENT}/scripts"

TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT

# Download the uber-JAR (a JAR is a ZIP archive)
curl -fsSL -o "${TMP_DIR}/${JAR_FILENAME}" "$JAR_URL" >&2

# Extract only the two script files from the nested path inside the JAR.
# -j  junk paths (extract flat into the target dir)
# -o  overwrite without prompting
mkdir -p "${TMP_DIR}/scripts"
unzip -j -o "${TMP_DIR}/${JAR_FILENAME}" \
    "eu/dnetlib/iis/wf/referenceextraction/covid19/main/oozie_app/lib/scripts/*" \
    -d "${TMP_DIR}/scripts" >&2

# Stage to HDFS (overwrite any previous version for the same snapshot)
hdfs dfs -rm -r -f "${SCRIPTS_HDFS_PATH}" >&2
hdfs dfs -mkdir -p "${SCRIPTS_HDFS_PARENT}" >&2
hdfs dfs -put "${TMP_DIR}/scripts" "${SCRIPTS_HDFS_PARENT}/" >&2

# This is the sole stdout line — BashOperator XCom captures it as return_value
echo "${SCRIPTS_HDFS_PATH}"
""",
            do_xcom_push=True,
        )

    # ---------------------------------------------------------------------------
    # Step 2: run the Spark job.  The scriptDirPath argument is resolved from
    # the XCom value produced by the extract_scripts task above.
    # Covid19ReferenceExtractionJob calls sc.addFile(scriptDirPath, recursive)
    # which distributes the HDFS scripts/ directory to every executor pod;
    # SparkFiles.get("scripts") then returns its absolute local path on each pod.
    # ---------------------------------------------------------------------------
    spark_task = generate_spark_operator(
        task_id="referenceextraction_covid19",
        task_display_name="Extract COVID-19 references using MADIS SQL matching",
        jar="{{ params.get('JAR') }}",
        image="{{ params.get('SPARK_IMAGE') }}",
        main_class="eu.dnetlib.iis.wf.referenceextraction.covid19.Covid19ReferenceExtractionJob",
        arguments=[
            "-inputAvroPath",             "{{ params.get('inputAvroPath') }}",
            "-outputAvroPath",            "{{ params.get('outputAvroPath') }}",
            "-predefinedConceptId",       "{{ params.get('predefinedConceptId') }}",
            "-predefinedConfidenceLevel", "{{ params.get('predefinedConfidenceLevel') }}",
            # "-scriptDirPath",             "{{ ti.xcom_pull(task_ids='extract_scripts_from_jar') }}",
            "-scriptDirPath",             "{{ params.get('scriptDirPath') }}",
            "-numberOfPartitions",        "{{ params.get('numberOfPartitions') }}",
            "-outputReportPath",          "{{ params.get('outputReportPath') }}",
        ],
        spark_extra_conf={
            "spark.driver.memory":   "{{ params.get('sparkDriverMemory') }}",
            "spark.executor.memory": "{{ params.get('sparkExecutorMemory') }}",
            "spark.driverEnv.HADOOP_USER_NAME":          "{{ params.get('HADOOP_USER_NAME') }}",
            "spark.executorEnv.HADOOP_USER_NAME":        "{{ params.get('HADOOP_USER_NAME') }}",
            "spark.driverEnv.SPARK_USER":                "{{ params.get('HADOOP_USER_NAME') }}",
            "spark.executorEnv.SPARK_USER":              "{{ params.get('HADOOP_USER_NAME') }}",
            "spark.kubernetes.driverEnv.HADOOP_USER_NAME":   "{{ params.get('HADOOP_USER_NAME') }}",
            "spark.kubernetes.executorEnv.HADOOP_USER_NAME": "{{ params.get('HADOOP_USER_NAME') }}",
        }
    )

    # extract_scripts >> spark_task


referenceextraction_covid19()
