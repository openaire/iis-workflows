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
        (skippable via the TEMPORARY ``skip_import_content_url`` param, so a test run can reuse the
        content urls already present under ``{workingDir}/import_content_url/imported``)
  * ``preprocessing`` (PIG, already ported)            → ``ChecksumPreprocessingJob``
  * ``check_input_isempty`` / ``decision_is_intput_empty``
        → absorbed by ``MetadataExtractionCacheChainJob`` which writes empty meta/fault datastores
          for empty input (the original Oozie branching existed only because of the cumbersome
          conditional transitions; the outcome is identical and much simpler).
  * ``metadata_extractor`` (prefetched = importer_content + core) **and**
    ``transformers_metadataextraction_skip_extracted``
        → both absorbed by ``MetadataExtractionCacheChainJob``: it takes the content URLs, drops
          the ids already present in the cache (the skip_extracted step, cache id resolved
          internally -- the Oozie ``get-existing-cache-id`` action is gone), then imports the PDF
          bytes in-process for the remaining subset only (``DocumentContentUrlBasedImporterJob``
          reused as a library, no intermediate datastore) and extracts via ``MetadataExtractorJob``.
  * ``cache_create`` / ``cache_update`` (lock + cache id + union + fault merge)
        → absorbed by ``MetadataExtractionCacheChainJob``.
  * ``report`` (report/builder workflow wrapping ProcessWrapper/ReportMerger)  → ``build_report``
        (``java_action`` running ``ProcessWrapper`` around ``ReportMerger``; the launcher,
        classpath and Hadoop configuration are the same as for the Spark tasks, so the Oozie
        action needs no bespoke shell script)
  * ``finalize`` / ``remove_sideproducts``             → left to cluster housekeeping (working
        directory retention policy); it is intentionally not modelled in Airflow.
"""

import os
import sys
from datetime import timedelta

from airflow.decorators import dag
from airflow.models.param import Param
from airflow.providers.standard.operators.empty import EmptyOperator
from airflow.providers.standard.operators.python import BranchPythonOperator
from airflow.providers.standard.operators.trigger_dagrun import TriggerDagRunOperator

package_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(package_dir)

for path in (package_dir, parent_dir):
    if path not in sys.path:
        sys.path.append(path)

from spark_configurator import generate_spark_operator, java_action

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


def _decide_import_content_url(params):
    """TEMPORARY testing switch: reuse the already imported content urls instead of re-importing."""
    if params.get("skip_import_content_url"):
        return ["skip_import_content_url"]
    return ["import_content_url"]


@dag(
    dag_id="spark_metadataextraction_cache_builder",
    dag_display_name="Metadata extraction cache: builder",
    default_args=default_args,
    params={
        # ---------------- artifacts / runtime ----------------
        "JAR_METADATAEXTRACTION": Param(
            default="https://maven.ceon.pl/artifactory/iis-snapshots/eu/dnetlib/iis/iis-wf-metadataextraction/1.3.0-SNAPSHOT/iis-wf-metadataextraction-1.3.0-20261113.181136-23-uber.jar",
            type="string",
            description="iis-wf-metadataextraction uber JAR (cache chain job incl. metadata extractor)",
        ),
        "JAR_TRANSFORMERS": Param(
            default="https://maven.ceon.pl/artifactory/iis-snapshots/eu/dnetlib/iis/iis-wf-transformers/1.3.0-SNAPSHOT/iis-wf-transformers-1.3.0-20261104.164945-19-uber.jar",
            type="string",
            description="iis-wf-transformers uber JAR (checksum preprocessing job)",
        ),
        "JAR_IMPORT": Param(
            default="https://maven.ceon.pl/artifactory/iis-snapshots/eu/dnetlib/iis/iis-wf-import/1.3.0-SNAPSHOT/iis-wf-import-1.3.0-20261109.165609-15-uber.jar",
            type="string",
            description="iis-wf-import uber JAR (content url importer + content url chain)",
        ),
        "JAR_REPORT": Param(
            default="https://maven.ceon.pl/artifactory/iis-snapshots/eu/dnetlib/iis/iis-wf-report/1.3.0-SNAPSHOT/iis-wf-report-1.3.0-20261113.155808-3-uber.jar",
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
        "input_table_name": Param(default="payload_data.payload", type="string"),
        "hive_metastore_uris": Param(default="thrift://iis-cdh5-test-m3.ocean.icm.edu.pl:9083", type="string"),
        "input_id_mapping": Param(
            default="hdfs://nameservice1/user/dnet.production/iis/working_dirs/primary/primary_import/metadataimport/identifiermapping", 
            type="string",
            description="Id mappings (eu.dnetlib.iis.common.schemas.IdentifierMapping) used by the "
                        "id-replacer to translate identifiers of deduplicated objects. "
                        "$UNDEFINED$ → the transformers_idreplacer step is skipped.",
        ),
        "input_id": Param(
            default="hdfs://nameservice1/tmp/marek.horst/metadataextraction/input_id/mini", 
            type="string",
            description="Identifiers existing in OA+ (eu.dnetlib.iis.common.schemas.Identifier) used "
                        "by the existence filter to drop contents without metadata representatives. "
                        "$UNDEFINED$ → the transformers_common_existencefilter step is skipped.",
        ),
        "mimetypes_pdf": Param(default="pdf,application/pdf", type="string"),
        "mimetypes_html": Param(default="text/html", type="string"),
        "mimetypes_xml_pmc": Param(default="xml,application/xml", type="string"),
        "mimetypes_wos": Param(default="file::WoS", type="string"),

        # TEMPORARY testing aid. The hive-based content url import is the slowest and most
        # expensive step of the chain, so a test run can reuse the datastores a previous run
        # already produced. Remove this switch (and the branch around import_content_url) once
        # the port is validated.
        "skip_import_content_url": Param(
            default=False,
            type="boolean",
            description="TEMPORARY/testing only: skip the import_content_url step "
                        "(spark_content_url_chain) and reuse the content url datastores already "
                        "present under {workingDir}/import_content_url/imported. Keep false for a "
                        "real run.",
        ),

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
        "grobid_server_url": Param(default="http://grobid.default:8070", type="string"),
        "grobid_server_version": Param(default="0.9.1", type="string"),
        "grobid_server_connection_timeout": Param(default="600000", type="string"),
        "grobid_server_read_timeout": Param(default="600000", type="string"),
        "grobid_server_throttle_sleep_time": Param(default="60000", type="string"),
        "grobid_server_max_retries_count": Param(default="10", type="string"),
        # ---------------- object store content retrieval ----------------
        # Values taken from the original Oozie job.properties shipped with this workflow
        # (iis-wf-metadataextraction/src/main/resources/.../cache/builder/job.properties).
        # Without the endpoint, every s3:// content URL fails with
        # S3EndpointNotFoundException ("no S3 client defined").
        "objectstore_s3_endpoint": Param(
            default="s3.icm.edu.pl",
            type="string",
            description="S3 (object store) service endpoint used to retrieve s3:// content URLs. "
                        "Required whenever the imported urls point to the object store.",
        ),
        "objectstore_s3_keystore_location": Param(
            default="jceks://hdfs/user/marek.horst/secrets/s3keyfile.jceks",
            type="string",
            description="Hadoop credential provider holding the fs.s3a.access.key / "
                        "fs.s3a.secret.key entries. When missing, anonymous credentials are used.",
        ),

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

        # ---------------- spark tuning ----------------
        "sparkDriverMemory": Param(
            default="7g",
            type="string",
            description="Memory for the driver. 7g mirrors the Hadoop cluster default for these "
                        "transformations.",
        ),
        "sparkExecutorMemory": Param(
            default="7g",
            type="string",
            description="Memory per executor. 7g mirrors the Hadoop cluster default for these "
                        "transformations (YARN ran 7g with 4 cores, ~1.75g per task).",
        ),
        "sparkExecutorCores": Param(
            default="4",
            type="string",
            description="Number of cores (and thus concurrent tasks) per executor. "
                        "The k8s cluster environment defaults to 24 cores, which starves each "
                        "task of heap and makes the Celeborn shuffle writers OOM. "
                        "Defaulting to 4 mirrors the Hadoop cluster where this workflow "
                        "succeeded (24 cores in k8s starves each task of heap).",
        ),
        # Applies ONLY to metadataextraction_cached -- the single task in this DAG that calls
        # Grobid. The other tasks are not pinned (see grobid_spark_conf below).
        "grobidSparkExecutorInstances": Param(
            default=8,
            type="integer",
            description="Fixed number of Spark executors for metadataextraction_cached. With "
                        "dynamic allocation disabled the number of concurrent tasks -- and "
                        "therefore the number of in-flight Grobid requests -- is predictable: "
                        "grobidSparkExecutorInstances x sparkExecutorCores. Lower it when Grobid "
                        "gets overloaded.",
        ),
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
        # Cores per executor control how many tasks share the executor heap concurrently.
        # The k8s environment sets 24 cores which starves each task of heap and makes the
        # Celeborn shuffle writers OOM. Defaulting to 4 mirrors the Hadoop cluster where
        # this workflow succeeded (24 cores in k8s starves each task of heap).
        "spark.executor.cores":          "{{ params.get('sparkExecutorCores') }}",
        "spark.executor.memoryOverhead": "{{ params.get('sparkExecutorOverhead') }}",
        **hadoop_user_conf,
    }

    # Grobid-facing configuration, applied ONLY to metadataextraction_cached -- the single job in
    # this DAG that calls the Grobid service. The other jobs neither contact Grobid nor gain
    # anything from being pinned, so they keep the environment's default dynamic allocation.
    grobid_spark_conf = {
        **spark_conf,
        # Fixed executor count and dynamic allocation always off: the number of concurrent tasks,
        # and therefore of in-flight Grobid requests, is then predictable
        # (grobidSparkExecutorInstances x sparkExecutorCores). Capping is the whole purpose of
        # this config, so dynamic allocation is deliberately NOT a tunable -- enabling it would
        # silently reintroduce the unbounded fan-out this guards against.
        "spark.executor.instances":        "{{ params.get('grobidSparkExecutorInstances') }}",
        "spark.dynamicAllocation.enabled": "false",
    }

    # SparkApplication exposes dynamic allocation as a first-class spec field whose properties
    # override sparkConf, and the operator's defaulting webhook enables it (with its own
    # maxExecutors) when the field is absent. Emit it explicitly to keep the executor count fixed.
    grobid_dynamic_allocation = {
        "enabled": False,
        "minExecutors": "1",
        "maxExecutors": "{{ params.get('grobidSparkExecutorInstances') }}",
        "shuffleTrackingEnabled": False,
    }

    imported_root    = "{{ params.get('workingDir') }}/import_content_url/imported"
    preprocessed_out = "{{ params.get('workingDir') }}/preprocessing/output"
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
            # optional transformations of the content url chain
            "input_id_mapping":  "{{ params.get('input_id_mapping') }}",
            "input_id":          "{{ params.get('input_id') }}",
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
            "sparkExecutorCores":    "{{ params.get('sparkExecutorCores') }}",
            "sparkExecutorOverhead": "{{ params.get('sparkExecutorOverhead') }}",
        },
        wait_for_completion=True,
        deferrable=False,
        poke_interval=60,
        allowed_states=["success"],
        failed_states=["failed"],
    )

    # TEMPORARY testing aid: either trigger the reusable content url chain DAG or reuse what a
    # previous run already imported. A BranchPythonOperator may only select its own direct
    # children, hence the explicit skip stub and the join below instead of wiring the branch
    # straight into ``preprocessing`` (which would then be skipped whenever the other branch ran).
    decide_import_content_url = BranchPythonOperator(
        task_id="decide_import_content_url",
        task_display_name="Import content urls? (temporary testing switch)",
        python_callable=_decide_import_content_url,
    )

    skip_import_content_url = EmptyOperator(
        task_id="skip_import_content_url",
        task_display_name="Reuse already imported content urls",
    )

    content_urls_ready = EmptyOperator(
        task_id="content_urls_ready",
        task_display_name="Content urls available",
        # exactly one of the two branches above completes, the other is skipped
        trigger_rule="none_failed",
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
    # 3. metadata_extractor + cache create/update                        #
    #
    # The content urls are the input: the job drops the already-cached ids (the Oozie
    # skip_extracted logic) and imports the PDF bytes IN-PROCESS for the remaining subset only
    # (the Oozie prefetched sub-workflow), then extracts and updates the cache. Doing all three
    # in one job avoids both a duplicated cache filter and a full-object-store import.
    # ------------------------------------------------------------------ #
    metadataextraction_cached = generate_spark_operator(
        task_id="metadataextraction_cached",
        task_display_name="Cached metadata extraction (MetadataExtractionCacheChainJob)",
        jar="{{ params.get('JAR_METADATAEXTRACTION') }}",
        image="{{ params.get('SPARK_IMAGE') }}",
        main_class="eu.dnetlib.iis.wf.metadataextraction.cache.MetadataExtractionCacheChainJob",
        arguments=[
            "-input",            preprocessed_out,
            "-outputRoot",       cache_out,
            "-outputNameMeta",   "{{ params.get('output_name_meta') }}",
            "-outputNameFault",  "{{ params.get('output_name_fault') }}",
            "-numberOfEmittedFiles", "{{ params.get('numberOfEmittedFiles') }}",
            "-cacheRootDir",     "{{ params.get('cache_location') }}",
            "-lockManagerFactoryClassName", "{{ params.get('lockManagerFactoryClassName') }}",
            "-numberOfEmittedFilesInCache", "{{ params.get('numberOfEmittedFilesInCache') }}",
            # content retrieval (in-process, applied to the not-yet-cached subset only)
            "-maxFileSizeMb",    "{{ params.get('max_file_size_mb') }}",
            "-contentConnectionTimeout", "{{ params.get('content_connection_timeout') }}",
            "-contentReadTimeout",       "{{ params.get('content_read_timeout') }}",
            "-objectstoreS3Endpoint",    "{{ params.get('objectstore_s3_endpoint') }}",
            "-objectstoreS3KeystoreLocation", "{{ params.get('objectstore_s3_keystore_location') }}",
            # builder does not return records already present in the cache
            # boolean options (JCommander arity 0) -- must use the '=' separator, otherwise the
            # flag is consumed on its own and the bare true/false token becomes an unknown
            # "main parameter"
            "-returnAlreadyExtractedMeta=false",
            "-activeMetadataExtraction=true",
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
        # the Grobid-concurrency guard: in-flight requests ~ instances x cores
        dynamic_allocation=grobid_dynamic_allocation,
        spark_extra_conf=grobid_spark_conf,
    )

    # ------------------------------------------------------------------ #
    # 6. build-report (report/builder workflow: ProcessWrapper+ReportMerger)
    #
    # ReportMerger implements eu.dnetlib.iis.common.java.Process and has no main(), so
    # ProcessWrapper stays the invoked class: it maps the Oozie-style -I<port>=/-O<port>=
    # arguments onto the process ports. java_action runs it via
    # eu.dnetlib.dhp.oozie.RunJavaSparkJob on the Spark driver, which resolves the uber JAR and
    # puts it on the classpath the same way as for the Spark jobs, and the Hadoop configuration
    # comes from the environment -- so the bespoke classpath/core-site.xml shell plumbing is gone.
    # ------------------------------------------------------------------ #
    build_report = java_action(
        task_id="build_report",
        task_display_name="Merge partial reports into report.json (ReportMerger)",
        jar="{{ params.get('JAR_REPORT') }}",
        main_class="eu.dnetlib.iis.common.java.ProcessWrapper",
        arguments=[
            "eu.dnetlib.iis.wf.report.ReportMerger",
            # ProcessWrapper ports: '-I<port>=<path>' / '-O<port>=<path>', each passed as a
            # single token that Commons CLI splits on the value separator
            "-Ipartial_reports={{ params.get('workingDir') }}/report",
            "-Oreport={{ params.get('workingDir') }}/report_builder/report.json",
        ],
        # write as the configured HDFS user, like the Spark tasks of this DAG
        spark_extra_conf=hadoop_user_conf,
    )

    # ------------------------------------------------------------------ #
    # 7. decision-copy-report + copy-report (distcp)                     #
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
    decide_import_content_url >> [import_content_url, skip_import_content_url]
    [import_content_url, skip_import_content_url] >> content_urls_ready
    content_urls_ready >> preprocessing >> metadataextraction_cached
    metadataextraction_cached >> build_report >> decision_copy_report
    decision_copy_report >> [copy_report, end]
    copy_report >> end


metadataextraction_cache_builder()
