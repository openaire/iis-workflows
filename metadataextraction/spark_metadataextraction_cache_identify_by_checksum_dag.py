"""
Metadata extraction cache ``identify_by_checksum`` — Airflow port of the Oozie workflow
``eu/dnetlib/iis/wf/metadataextraction/cache/identify_by_checksum/oozie_app``.

This is one of the top level metadata extraction workflows (used by the primary IIS workflow)
and therefore has to stay an Airflow DAG.

Original Oozie graph::

    preprocessing ─ metadataextraction_cached ─┬ postprocessing-meta  ─┐
                                               └ postprocessing-fault ─┴ end

Airflow mapping
---------------
  * ``preprocessing`` (PIG, already ported)            → ``ChecksumPreprocessingJob``
  * ``metadataextraction_cached`` (cache chain +
    cache create/update, lock + cache id management)   → ``MetadataExtractionCacheChainJob``
    The whole Oozie cache hierarchy is offloaded into this single Spark job (following the
    ``CachedWebCrawlerJob``/``CacheStorageUtils`` pattern) instead of being reproduced as a
    set of airflow workflows: the cache management logic is not reused anywhere else and its
    conditional transitions / ZooKeeper lock utility actions are much cleaner inside Spark.
  * ``metadata_extractor`` (prefetched = importer_content + core)
        - ``importer_content`` (MapReduce, object store)  → ``DocumentContentUrlBasedImporterJob``
        - ``core`` (``MetadataExtractorMapper``)          → ``MetadataExtractorJob`` (invoked
          in-process by ``MetadataExtractionCacheChainJob``)
  * ``postprocessing-meta`` (PIG, already ported)      → ``ChecksumMetaPostprocessingJob``
  * ``postprocessing-fault`` (PIG, already ported)     → ``ChecksumFaultPostprocessingJob``
"""

import os
import sys
from datetime import timedelta

from airflow.decorators import dag
from airflow.models.param import Param
from airflow.providers.standard.operators.empty import EmptyOperator

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


@dag(
    dag_id="spark_metadataextraction_cache_identify_by_checksum",
    dag_display_name="Metadata extraction cache: identify by checksum",
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
            description="iis-wf-transformers uber JAR (checksum preprocessing/postprocessing jobs)",
        ),
        "JAR_IMPORT": Param(
            default="https://maven.ceon.pl/artifactory/iis-snapshots/eu/dnetlib/iis/iis-wf-import/1.3.0-SNAPSHOT/iis-wf-import-1.3.0-uber.jar",
            type="string",
            description="iis-wf-import uber JAR (content url based importer)",
        ),
        "SPARK_IMAGE": Param(
            "docker-registry.openaire.eu/kubernetes_devel/spark:4.1.2", type="string"),
        "HADOOP_USER_NAME": Param("marek.horst", type="string"),

        # ---------------- input / output ----------------
        "input": Param(
            default="hdfs://nameservice1/tmp/marek.horst/metadataextraction/input/pdf/micro/",
            type="string",
            description="Input DocumentContentUrl datastore",
        ),
        "output_root": Param(
            default="hdfs://nameservice1/tmp/marek.horst/metadataextraction/identify_by_checksum/out",
            type="string",
            description="Metadata extraction output root directory (holds meta and fault subdirs)",
        ),
        "workingDir": Param(
            default="hdfs://nameservice1/tmp/marek.horst/metadataextraction/identify_by_checksum",
            type="string",
            description="Working directory for intermediate datastores",
        ),
        "output_name_meta": Param(default="meta", type="string"),
        "output_name_fault": Param(default="fault", type="string"),
        "output_name_transient_fault": Param(default="transientfault", type="string"),

        # ---------------- metadata extraction ----------------
        "excluded_ids": Param(
            default=_UNDEFINED, type="string",
            description="CSV of content identifiers (checksums) excluded from processing",
        ),
        "max_file_size_mb": Param(default=_UNDEFINED, type="string",
                                  description="Maximum allowed content size in MB"),
        "content_connection_timeout": Param(default="60000", type="string"),
        "content_read_timeout": Param(default="60000", type="string"),
        "log_fault_processing_time_threshold_secs": Param(default="60", type="string"),
        "interrupt_processing_time_threshold_secs": Param(default="120", type="string"),

        # ---------------- grobid ----------------
        "grobid_server_url": Param(
            default=_UNDEFINED, type="string",
            description="Grobid server address; $UNDEFINED$ → embedded CERMINE is used",
        ),
        "grobid_server_version": Param(default=_UNDEFINED, type="string"),
        "grobid_server_connection_timeout": Param(default="600000", type="string"),
        "grobid_server_read_timeout": Param(default="600000", type="string"),
        "grobid_server_throttle_sleep_time": Param(default="60000", type="string"),
        "grobid_server_max_retries_count": Param(default="10", type="string"),

        # ---------------- object store content retrieval ----------------
        "objectstore_s3_endpoint": Param(default=_UNDEFINED, type="string"),
        "objectstore_s3_keystore_location": Param(default=_UNDEFINED, type="string"),

        # ---------------- cache ----------------
        "cache_location": Param(
            default="hdfs://nameservice1/tmp/marek.horst/metadataextraction/cache",
            type="string",
            description="Metadata extraction HDFS cache location",
        ),
        "lockManagerFactoryClassName": Param(
            default="eu.dnetlib.iis.common.lock.HadoopFsLockManagerFactory",
            type="string",
            description="Lock manager factory used while updating the cache. "
                        "HadoopFsLockManagerFactory needs no external coordination service; "
                        "use eu.dnetlib.iis.common.lock.ZookeeperLockManagerFactory for legacy "
                        "Hadoop HA clusters.",
        ),
        "numberOfEmittedFilesInCache": Param(default="1000", type="string"),
        "numberOfEmittedFiles": Param(default="1000", type="string"),

        # ---------------- reports ----------------
        "output_report_root_path": Param(
            default="", type="string",
            description="Base directory for partial reports. Empty → {workingDir}/report",
        ),
        "cache_report_relative_path": Param(default="import_metadataextraction_cache", type="string"),
        "cache_report_properties_prefix": Param(default="import.metadataExtraction", type="string"),

        # ---------------- spark tuning ----------------
        "sparkDriverMemory": Param(default="4g", type="string"),
        "sparkExecutorMemory": Param(default="4g", type="string"),
        "sparkExecutorOverhead": Param(default="1024", type="string"),
    },
    tags=["openaire", "iis", "metadataextraction", "cache"],
    schedule=None,
)
def metadataextraction_cache_identify_by_checksum():
    """preprocessing → cached metadata extraction → postprocessing (meta/fault)."""

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

    report_root = "{{ params.get('output_report_root_path') or (params.get('workingDir') + '/report') }}"
    preprocessed = "{{ params.get('workingDir') }}/preprocessing/output"
    imported_content = "{{ params.get('workingDir') }}/importer_content/output"
    cache_out = "{{ params.get('workingDir') }}/metadataextraction_cached/out"

    # ------------------------------------------------------------------ #
    # 1. preprocessing (id → contentChecksum)                            #
    # ------------------------------------------------------------------ #
    preprocessing = generate_spark_operator(
        task_id="preprocessing",
        task_display_name="Replace id with content checksum (ChecksumPreprocessingJob)",
        jar="{{ params.get('JAR_TRANSFORMERS') }}",
        image="{{ params.get('SPARK_IMAGE') }}",
        main_class="eu.dnetlib.iis.wf.transformers.metadataextraction.checksum.preprocessing.ChecksumPreprocessingJob",
        arguments=[
            "-input",  "{{ params.get('input') }}",
            "-output", preprocessed,
        ],
        spark_extra_conf=spark_conf,
    )

    # ------------------------------------------------------------------ #
    # 2. importer_content (DocumentContentUrl → DocumentContent bytes)   #
    #    (Oozie metadataextraction/prefetched → importer_content)        #
    # ------------------------------------------------------------------ #
    importer_content = generate_spark_operator(
        task_id="importer_content",
        task_display_name="Retrieve content bytes (DocumentContentUrlBasedImporterJob)",
        jar="{{ params.get('JAR_IMPORT') }}",
        image="{{ params.get('SPARK_IMAGE') }}",
        main_class="eu.dnetlib.iis.wf.importer.content.DocumentContentUrlBasedImporterJob",
        arguments=[
            "-input",            preprocessed,
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
    # 3. metadataextraction_cached (cache chain: skip_extracted →        #
    #    cache create/update → metadata extractor → union + faults)      #
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
            # identify_by_checksum chain: return records already present in the cache
            "-returnAlreadyExtractedMeta", "true",
            "-activeMetadataExtraction",   "false",
            "-outputReportRootPath",       report_root,
            "-cacheReportRelativePath",    "{{ params.get('cache_report_relative_path') }}",
            "-extractionReportRelativePath", "import_metadataextraction",
            "-reportPropertiesPrefix",     "{{ params.get('cache_report_properties_prefix') }}",
            "-excludedIds",                "{{ params.get('excluded_ids') }}",
            "-logFaultProcessingTimeThresholdSecs",      "{{ params.get('log_fault_processing_time_threshold_secs') }}",
            "-interruptProcessingTimeThresholdSecs",     "{{ params.get('interrupt_processing_time_threshold_secs') }}",
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
    # 4. postprocessing (checksum → OA+ identifier)                      #
    # ------------------------------------------------------------------ #
    postprocessing_meta = generate_spark_operator(
        task_id="postprocessing-meta",
        task_display_name="Restore OA+ identifiers in extracted metadata (ChecksumMetaPostprocessingJob)",
        jar="{{ params.get('JAR_TRANSFORMERS') }}",
        image="{{ params.get('SPARK_IMAGE') }}",
        main_class="eu.dnetlib.iis.wf.transformers.metadataextraction.checksum.postprocessing.meta.ChecksumMetaPostprocessingJob",
        arguments=[
            "-inputExtractedDocumentMetadata", cache_out + "/{{ params.get('output_name_meta') }}",
            "-inputDocumentContentUrl",        preprocessed,
            "-output",                         "{{ params.get('output_root') }}/{{ params.get('output_name_meta') }}",
        ],
        spark_extra_conf=spark_conf,
    )

    postprocessing_fault = generate_spark_operator(
        task_id="postprocessing-fault",
        task_display_name="Restore OA+ identifiers in faults (ChecksumFaultPostprocessingJob)",
        jar="{{ params.get('JAR_TRANSFORMERS') }}",
        image="{{ params.get('SPARK_IMAGE') }}",
        main_class="eu.dnetlib.iis.wf.transformers.metadataextraction.checksum.postprocessing.fault.ChecksumFaultPostprocessingJob",
        arguments=[
            "-inputFault",               cache_out + "/{{ params.get('output_name_fault') }}",
            "-inputDocumentContentUrl",  preprocessed,
            "-output",                   "{{ params.get('output_root') }}/{{ params.get('output_name_fault') }}",
        ],
        spark_extra_conf=spark_conf,
    )

    end = EmptyOperator(task_id="end", task_display_name="Metadata extraction finished")

    # ------------------------------------------------------------------ #
    # Wiring                                                             #
    # ------------------------------------------------------------------ #
    preprocessing >> importer_content >> metadataextraction_cached
    metadataextraction_cached >> [postprocessing_meta, postprocessing_fault]
    [postprocessing_meta, postprocessing_fault] >> end


metadataextraction_cache_identify_by_checksum()
