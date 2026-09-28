"""
Metadata extraction cache ``builder`` — Airflow port of the Oozie workflow
``eu/dnetlib/iis/wf/metadataextraction/cache/builder/oozie_app``.

This is the top level workflow responsible for performing cache updates and therefore has to stay
an Airflow DAG.

Original Oozie graph::

    init-workingDir ─ copy-version ─ import_content_url ─ preprocessing ─ check_input_isempty
        ├ (empty)  create-empty-report ─────────────────────────────┐
        └ (not empty) transformers_metadataextraction_skip_extracted┐
                       ├ cache_create  ─┐                            │
                       └ cache_update  ─┴ build-report ─ finalize ─ end

Airflow mapping
---------------
  * ``import_content_url`` (reusable workflow)         → triggers the ``spark_content_url_chain`` DAG
  * ``preprocessing`` (PIG, already ported)            → ``ChecksumPreprocessingJob``
  * ``check_input_isempty`` / ``decision_is_intput_empty``
        → absorbed by ``MetadataExtractionCacheChainJob`` which writes empty meta/fault datastores
          for empty input (the original Oozie branching existed only because of the cumbersome
          conditional transitions; the outcome is identical and much simpler).
  * ``metadata_extractor`` (prefetched = importer_content + core)
        - ``importer_content`` (MapReduce, object store)  → ``DocumentContentUrlBasedImporterJob``
        - ``core`` (``MetadataExtractorMapper``)          → ``MetadataExtractorJob`` (invoked
          in-process by ``MetadataExtractionCacheChainJob``)
  * ``transformers_metadataextraction_skip_extracted`` (PIG, already ported)
        → absorbed by ``MetadataExtractionCacheChainJob`` (``-returnAlreadyExtractedMeta=false``,
          i.e. the ``skip_extracted_without_meta`` variant used by the builder).
  * ``cache_create`` / ``cache_update`` (lock + cache id + union + fault merge)
        → absorbed by ``MetadataExtractionCacheChainJob``.
  * ``report`` (report/builder workflow wrapping ProcessWrapper/ReportMerger)  → ``build_report``
        (KubernetesPodOperator running ProcessWrapper around ``ReportMerger``, the same pattern as
        ``primary/export_dag.py``).
  * ``finalize`` / ``remove_sideproducts``             → left to cluster housekeeping (working
        directory retention policy); it is intentionally not modelled in Airflow.
"""

import os
import sys
from datetime import timedelta

from airflow.decorators import dag
from airflow.models.param import Param
from airflow.providers.cncf.kubernetes.operators.pod import KubernetesPodOperator
from airflow.providers.standard.operators.empty import EmptyOperator
from airflow.providers.standard.operators.python import BranchPythonOperator
from airflow.providers.standard.operators.trigger_dagrun import TriggerDagRunOperator

package_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(package_dir)

for path in (package_dir, parent_dir):
    if path not in sys.path:
        sys.path.append(path)

from spark_configurator import generate_spark_operator

EXECUTION_TIMEOUT = int(os.getenv("EXECUTION_TIMEOUT", 6))

default_args = {
    "execution_timeout": timedelta(days=EXECUTION_TIMEOUT),
    "retries": int(os.getenv("DEFAULT_TASK_RETRIES", 1)),
    "retry_delay": timedelta(seconds=int(os.getenv("DEFAULT_RETRY_DELAY_SECONDS", 60))),
}

_UNDEFINED = "$UNDEFINED$"

_CONTENT_URL_CHAIN_DAG_ID = "spark_content_url_chain"
_DISTCP_DAG_ID = "spark_distcp"


def _decision_copy_report(params):
    external = params.get("reports_external_path")
    if external and external != _UNDEFINED:
        return ["copy_report"]
    return ["end"]


@dag(
    dag_id="spark_metadataextraction_cache_builder",
    dag_display_name="Metadata extraction cache: builder",
    default_args=default_args,
    params={
        # ---------------- artifacts / runtime ----------------
        "JAR_METADATAEXTRACTION": Param(
            default="https://maven.ceon.pl/artifactory/iis-snapshots/eu/dnetlib/iis/iis-wf-metadataextraction/1.3.0-SNAPSHOT/iis-wf-metadataextraction-1.3.0-uber.jar",
            type="string",
            description="iis-wf-metadataextraction uber JAR (cache chain job incl. metadata extractor)",
        ),
        "JAR_TRANSFORMERS": Param(
            default="https://maven.ceon.pl/artifactory/iis-snapshots/eu/dnetlib/iis/iis-wf-transformers/1.3.0-SNAPSHOT/iis-wf-transformers-1.3.0-uber.jar",
            type="string",
            description="iis-wf-transformers uber JAR (checksum preprocessing job)",
        ),
        "JAR_IMPORT": Param(
            default="https://maven.ceon.pl/artifactory/iis-snapshots/eu/dnetlib/iis/iis-wf-import/1.3.0-SNAPSHOT/iis-wf-import-1.3.0-uber.jar",
            type="string",
            description="iis-wf-import uber JAR (content url importer + content url chain)",
        ),
        "JAR_REPORT": Param(
            default="https://maven.ceon.pl/artifactory/iis-snapshots/eu/dnetlib/iis/iis-wf-report/1.3.0-SNAPSHOT/iis-wf-report-1.3.0-uber.jar",
            type="string",
            description="iis-wf-report uber JAR (ReportMerger)",
        ),
        "SPARK_IMAGE": Param(
            "docker-registry.openaire.eu/kubernetes_devel/spark:4.1.2", type="string"),
        "HADOOP_USER_NAME": Param("marek.horst", type="string"),

        # ---------------- import (hive aggregation subsystem) ----------------
        "import_mode": Param(
            default="pdf", type="string",
            description="Import mode selecting the content url chain output subdirectory: pdf│xmlpmc",
        ),
        "input_table_name": Param(default="", type="string"),
        "hive_metastore_uris": Param(default="", type="string"),
        "mimetypes_pdf": Param(default="pdf,application/pdf", type="string"),
        "mimetypes_html": Param(default="text/html", type="string"),
        "mimetypes_xml_pmc": Param(default="xml,application/xml", type="string"),
        "mimetypes_wos": Param(default="file::WoS", type="string"),

        # ---------------- working dir / output ----------------
        "workingDir": Param(
            default="hdfs://nameservice1/tmp/marek.horst/metadataextraction/cache_builder",
            type="string",
            description="Working directory (holding import_content_url, preprocessing, "
                        "metadata_extractor and report subdirectories)",
        ),
        "output_name_meta": Param(default="meta", type="string"),
        "output_name_fault": Param(default="fault", type="string"),
        "output_name_transient_fault": Param(default="transientfault", type="string"),
        "reports_external_path": Param(
            default=_UNDEFINED, type="string",
            description="Optional directory storing reports from different executions. "
                        "$UNDEFINED$ → copying is skipped.",
        ),

        # ---------------- metadata extraction ----------------
        "metadataextraction_excluded_checksums": Param(
            default=_UNDEFINED, type="string",
            description="CSV of content checksums excluded from processing",
        ),
        "max_file_size_mb": Param(default="100", type="string",
                                  description="Maximum allowed content size in MB"),
        "content_connection_timeout": Param(default="60000", type="string"),
        "content_read_timeout": Param(default="60000", type="string"),
        "log_fault_processing_time_threshold_secs": Param(default="60", type="string"),
        "interrupt_processing_time_threshold_secs": Param(default="120", type="string"),
        "grobid_server_url": Param(default=_UNDEFINED, type="string"),
        "grobid_server_version": Param(default=_UNDEFINED, type="string"),
        "grobid_server_connection_timeout": Param(default="600000", type="string"),
        "grobid_server_read_timeout": Param(default="600000", type="string"),
        "grobid_server_throttle_sleep_time": Param(default="60000", type="string"),
        "grobid_server_max_retries_count": Param(default="10", type="string"),
        "objectstore_s3_endpoint": Param(default=_UNDEFINED, type="string"),
        "objectstore_s3_keystore_location": Param(default=_UNDEFINED, type="string"),

        # ---------------- cache ----------------
        "cache_location": Param(
            default="hdfs://nameservice1/tmp/marek.horst/metadataextraction/cache",
            type="string",
            description="Metadata extraction HDFS cache location",
        ),
        "lockManagerFactoryClassName": Param(
            default="eu.dnetlib.iis.common.lock.HadoopFsLockManagerFactory", type="string"),
        "numberOfEmittedFilesInCache": Param(default="1000", type="string"),
        "numberOfEmittedFiles": Param(default="1000", type="string"),

        # ---------------- utility pod HDFS access ----------------
        "HDFS_NAMENODE": Param(default="hdfs://nameservice1", type="string"),
        "HADOOP_CONF_DIR": Param(default="/opt/hadoop/etc/hadoop", type="string"),

        # ---------------- spark tuning ----------------
        "sparkDriverMemory": Param(default="4g", type="string"),
        "sparkExecutorMemory": Param(default="4g", type="string"),
        "sparkExecutorOverhead": Param(default="1024", type="string"),
    },
    tags=["openaire", "iis", "metadataextraction", "cache"],
    schedule=None,
)
def metadataextraction_cache_builder():
    """import_content_url → preprocessing → import content → cached metadata extraction → report."""

    hadoop_user_conf = {
        "spark.driverEnv.HADOOP_USER_NAME":              "{{ params.get('HADOOP_USER_NAME') }}",
        "spark.executorEnv.HADOOP_USER_NAME":            "{{ params.get('HADOOP_USER_NAME') }}",
        "spark.driverEnv.SPARK_USER":                    "{{ params.get('HADOOP_USER_NAME') }}",
        "spark.executorEnv.SPARK_USER":                  "{{ params.get('HADOOP_USER_NAME') }}",
        "spark.kubernetes.driverEnv.HADOOP_USER_NAME":   "{{ params.get('HADOOP_USER_NAME') }}",
        "spark.kubernetes.executorEnv.HADOOP_USER_NAME": "{{ params.get('HADOOP_USER_NAME') }}",
    }

    spark_conf = {
        "spark.driver.memory":           "{{ params.get('sparkDriverMemory') }}",
        "spark.executor.memory":         "{{ params.get('sparkExecutorMemory') }}",
        "spark.executor.memoryOverhead": "{{ params.get('sparkExecutorOverhead') }}",
        **hadoop_user_conf,
    }

    imported_root    = "{{ params.get('workingDir') }}/import_content_url/imported"
    preprocessed_out = "{{ params.get('workingDir') }}/preprocessing/output"
    imported_content = "{{ params.get('workingDir') }}/importer_content/output"
    cache_out        = "{{ params.get('workingDir') }}/metadata_extractor/output_root"
    report_root      = "{{ params.get('workingDir') }}/report"

    # ------------------------------------------------------------------ #
    # 1. import_content_url (reusable content url chain DAG)              #
    # ------------------------------------------------------------------ #
    import_content_url = TriggerDagRunOperator(
        task_id="import_content_url",
        task_display_name="Import content urls (spark_content_url_chain)",
        trigger_dag_id=_CONTENT_URL_CHAIN_DAG_ID,
        conf={
            "JAR_IMPORT":        "{{ params.get('JAR_IMPORT') }}",
            "JAR_TRANSFORMERS":  "{{ params.get('JAR_TRANSFORMERS') }}",
            "SPARK_IMAGE":       "{{ params.get('SPARK_IMAGE') }}",
            "HADOOP_USER_NAME":  "{{ params.get('HADOOP_USER_NAME') }}",
            "input_table_name":  "{{ params.get('input_table_name') }}",
            "hive_metastore_uris": "{{ params.get('hive_metastore_uris') }}",
            "mimetypes_pdf":     "{{ params.get('mimetypes_pdf') }}",
            "mimetypes_html":    "{{ params.get('mimetypes_html') }}",
            "mimetypes_xml_pmc": "{{ params.get('mimetypes_xml_pmc') }}",
            "mimetypes_wos":     "{{ params.get('mimetypes_wos') }}",
            "workingDir":        "{{ params.get('workingDir') }}/import_content_url/working_dir",
            "output_root":       imported_root,
            "output_name_pdf":   "pdf",
            "output_name_xml_pmc": "xmlpmc",
            "output_report_root_path": report_root,
            "sparkDriverMemory":     "{{ params.get('sparkDriverMemory') }}",
            "sparkExecutorMemory":   "{{ params.get('sparkExecutorMemory') }}",
            "sparkExecutorOverhead": "{{ params.get('sparkExecutorOverhead') }}",
        },
        wait_for_completion=True,
        deferrable=False,
        poke_interval=60,
        allowed_states=["success"],
        failed_states=["failed"],
    )

    # ------------------------------------------------------------------ #
    # 2. preprocessing (id → contentChecksum)                            #
    # ------------------------------------------------------------------ #
    preprocessing = generate_spark_operator(
        task_id="preprocessing",
        task_display_name="Replace id with content checksum (ChecksumPreprocessingJob)",
        jar="{{ params.get('JAR_TRANSFORMERS') }}",
        image="{{ params.get('SPARK_IMAGE') }}",
        main_class="eu.dnetlib.iis.wf.transformers.metadataextraction.checksum.preprocessing.ChecksumPreprocessingJob",
        arguments=[
            "-input",  imported_root + "/{{ params.get('import_mode') }}",
            "-output", preprocessed_out,
        ],
        spark_extra_conf=spark_conf,
    )

    # ------------------------------------------------------------------ #
    # 3. importer_content (DocumentContentUrl → DocumentContent bytes)    #
    # ------------------------------------------------------------------ #
    importer_content = generate_spark_operator(
        task_id="importer_content",
        task_display_name="Retrieve content bytes (DocumentContentUrlBasedImporterJob)",
        jar="{{ params.get('JAR_IMPORT') }}",
        image="{{ params.get('SPARK_IMAGE') }}",
        main_class="eu.dnetlib.iis.wf.importer.content.DocumentContentUrlBasedImporterJob",
        arguments=[
            "-input",            preprocessed_out,
            "-output",           imported_content,
            "-outputReportPath", report_root + "/import_content_pdf",
            "-maxFileSizeMb",    "{{ params.get('max_file_size_mb') }}",
            "-contentConnectionTimeout", "{{ params.get('content_connection_timeout') }}",
            "-contentReadTimeout",       "{{ params.get('content_read_timeout') }}",
            "-objectstoreS3Endpoint",    "{{ params.get('objectstore_s3_endpoint') }}",
            "-objectstoreS3KeystoreLocation", "{{ params.get('objectstore_s3_keystore_location') }}",
        ],
        spark_extra_conf=spark_conf,
    )

    # ------------------------------------------------------------------ #
    # 4. metadata_extractor + cache create/update                        #
    #    (skip_extracted_without_meta: nothing is returned from cache)    #
    # ------------------------------------------------------------------ #
    metadataextraction_cached = generate_spark_operator(
        task_id="metadataextraction_cached",
        task_display_name="Cached metadata extraction (MetadataExtractionCacheChainJob)",
        jar="{{ params.get('JAR_METADATAEXTRACTION') }}",
        image="{{ params.get('SPARK_IMAGE') }}",
        main_class="eu.dnetlib.iis.wf.metadataextraction.cache.MetadataExtractionCacheChainJob",
        arguments=[
            "-input",            imported_content,
            "-outputRoot",       cache_out,
            "-outputNameMeta",   "{{ params.get('output_name_meta') }}",
            "-outputNameFault",  "{{ params.get('output_name_fault') }}",
            "-numberOfEmittedFiles", "{{ params.get('numberOfEmittedFiles') }}",
            "-cacheRootDir",     "{{ params.get('cache_location') }}",
            "-lockManagerFactoryClassName", "{{ params.get('lockManagerFactoryClassName') }}",
            "-numberOfEmittedFilesInCache", "{{ params.get('numberOfEmittedFilesInCache') }}",
            # builder uses the skip_extracted_without_meta variant: does not return cached records
            "-returnAlreadyExtractedMeta", "false",
            "-activeMetadataExtraction",   "true",
            "-outputReportRootPath",       report_root,
            "-cacheReportRelativePath",    "import_metadataextraction_cache",
            "-extractionReportRelativePath", "import_metadataextraction",
            "-reportPropertiesPrefix",     "import.metadataExtraction",
            "-excludedIds",                "{{ params.get('metadataextraction_excluded_checksums') }}",
            "-logFaultProcessingTimeThresholdSecs",  "{{ params.get('log_fault_processing_time_threshold_secs') }}",
            "-interruptProcessingTimeThresholdSecs", "{{ params.get('interrupt_processing_time_threshold_secs') }}",
            "-grobidServerUrl",            "{{ params.get('grobid_server_url') }}",
            "-grobidServerVersion",        "{{ params.get('grobid_server_version') }}",
            "-grobidServerConnectionTimeout", "{{ params.get('grobid_server_connection_timeout') }}",
            "-grobidServerReadTimeout",       "{{ params.get('grobid_server_read_timeout') }}",
            "-grobidServerThrottleSleepTime", "{{ params.get('grobid_server_throttle_sleep_time') }}",
            "-grobidServerMaxRetriesCount",   "{{ params.get('grobid_server_max_retries_count') }}",
        ],
        spark_extra_conf=spark_conf,
    )

    # ------------------------------------------------------------------ #
    # 5. build-report (report/builder workflow: ProcessWrapper+ReportMerger)
    # ------------------------------------------------------------------ #
    build_report = KubernetesPodOperator(
        task_id="build_report",
        task_display_name="Merge partial reports into report.json (ReportMerger)",

        name="build-report-{{ ds }}-{{ task_instance.try_number }}",
        namespace="spark-jobs",
        kubernetes_conn_id="kubernetes_default",

        image="{{ params.get('SPARK_IMAGE') }}",
        image_pull_policy="Always",

        cmds=["/bin/bash"],
        arguments=["-c", r"""
set -euo pipefail

JAR_URL="{{ params.get('JAR_REPORT') }}"
JAR_FILENAME=$(basename "$JAR_URL")
TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT

export HADOOP_USER_NAME="{{ params.get('HADOOP_USER_NAME') }}"

echo "[STEP 1] Downloading uber-JAR"
wget -q -O "${TMP_DIR}/${JAR_FILENAME}" "$JAR_URL"

echo "[STEP 2] Resolving Hadoop configuration"
HADOOP_CONF_DIR='{{ params.get('HADOOP_CONF_DIR') }}'
if [ -f "${HADOOP_CONF_DIR}/core-site.xml" ]; then
    echo "  Using mounted Hadoop config from ${HADOOP_CONF_DIR}"
    CP_CONF="${HADOOP_CONF_DIR}"
else
    echo "  Generating minimal core-site.xml (fs.defaultFS)"
    mkdir -p "${TMP_DIR}/conf"
    cat > "${TMP_DIR}/conf/core-site.xml" << 'CORE_XML'
<?xml version="1.0"?>
<?xml-stylesheet type="text/xsl" href="configuration.xsl"?>
<configuration>
    <property>
        <name>fs.defaultFS</name>
        <value>{{ params.get('HDFS_NAMENODE') }}</value>
    </property>
</configuration>
CORE_XML
    CP_CONF="${TMP_DIR}/conf"
fi

echo "[STEP 3] Running ReportMerger"
cd "${TMP_DIR}"
SPARK_CP="/opt/spark/jars/*"

java \
    -Djava.io.tmpdir="${TMP_DIR}" \
    -cp "${CP_CONF}:${SPARK_CP}:${TMP_DIR}/${JAR_FILENAME}" \
    eu.dnetlib.iis.common.java.ProcessWrapper \
    eu.dnetlib.iis.wf.report.ReportMerger \
    -Ipartial_reports={{ params.get('workingDir') }}/report \
    -Oreport={{ params.get('workingDir') }}/report_builder/report.json

echo "[DONE] Report merged successfully"
        """],

        env_vars={
            "HADOOP_USER_NAME": "{{ params.get('HADOOP_USER_NAME') }}",
        },

        startup_timeout_seconds=300,
        get_logs=True,
        is_delete_operator_pod=True,
    )

    # ------------------------------------------------------------------ #
    # 6. decision-copy-report + copy-report (distcp)                     #
    # ------------------------------------------------------------------ #
    decision_copy_report = BranchPythonOperator(
        task_id="decision_copy_report",
        python_callable=_decision_copy_report,
    )

    copy_report = TriggerDagRunOperator(
        task_id="copy_report",
        task_display_name="Copy report to the external location (spark_distcp)",
        trigger_dag_id=_DISTCP_DAG_ID,
        conf={
            "FROM": ["{{ params.get('workingDir') }}/report_builder/report.json"],
            "TO": "{{ params.get('reports_external_path') }}/report_{{ ts_nodash }}.json",
            "OVERWRITE": True,
        },
        wait_for_completion=True,
        deferrable=False,
        poke_interval=60,
        allowed_states=["success"],
        failed_states=["failed"],
    )

    end = EmptyOperator(task_id="end", task_display_name="Cache builder finished",
                        trigger_rule="none_failed")

    # ------------------------------------------------------------------ #
    # Wiring                                                             #
    # ------------------------------------------------------------------ #
    import_content_url >> preprocessing >> importer_content >> metadataextraction_cached
    metadataextraction_cached >> build_report >> decision_copy_report
    decision_copy_report >> [copy_report, end]
    copy_report >> end


metadataextraction_cache_builder()
