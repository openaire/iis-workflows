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
  * ``metadataextraction_cached`` (cache lookup only)
        → ``MetadataExtractionCacheChainJob`` on the preprocessed ``DocumentContentUrl`` datastore
          with ``-activeMetadataExtraction=false``: the job drops the already-cached ids and returns
          the cached metadata. No content is imported and nothing is extracted, so the Oozie
          ``prefetched`` sub-workflow (importer_content + core) has no counterpart here.
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
        # This workflow only reads the cache (activeMetadataExtraction=false): it performs no
        # extraction and imports no content bytes, so all Grobid / content-retrieval settings are
        # gone. The chain job falls back to its built-in defaults for them.
        "excluded_ids": Param(
            default=_UNDEFINED, type="string",
            description="CSV of content identifiers (checksums) excluded from processing",
        ),
        "log_fault_processing_time_threshold_secs": Param(default="60", type="string"),
        "interrupt_processing_time_threshold_secs": Param(default="120", type="string"),

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
        # Cores per executor control how many tasks share the executor heap concurrently.
        # The k8s environment sets 24 cores which starves each task of heap and makes the
        # Celeborn shuffle writers OOM. Defaulting to 4 mirrors the Hadoop cluster where
        # this workflow succeeded (24 cores in k8s starves each task of heap).
        "spark.executor.cores":          "{{ params.get('sparkExecutorCores') }}",
        "spark.executor.memoryOverhead": "{{ params.get('sparkExecutorOverhead') }}",
        **hadoop_user_conf,
    }

    report_root = "{{ params.get('output_report_root_path') or (params.get('workingDir') + '/report') }}"
    preprocessed = "{{ params.get('workingDir') }}/preprocessing/output"
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
    # 2. metadataextraction_cached (cache chain: cache id resolution +    #
    #    cache lookup → union + faults)                                  #
    #                                                                    #
    # No importer_content step: extraction is disabled here, so the PDF  #
    # bytes are never needed -- only the identifiers of the documents    #
    # (the preprocessed DocumentContentUrl datastore).                   #
    # ------------------------------------------------------------------ #
    metadataextraction_cached = generate_spark_operator(
        task_id="metadataextraction_cached",
        task_display_name="Cached metadata extraction (MetadataExtractionCacheChainJob)",
        jar="{{ params.get('JAR_METADATAEXTRACTION') }}",
        image="{{ params.get('SPARK_IMAGE') }}",
        main_class="eu.dnetlib.iis.wf.metadataextraction.cache.MetadataExtractionCacheChainJob",
        arguments=[
            "-input",            preprocessed,
            "-outputRoot",       cache_out,
            "-outputNameMeta",   "{{ params.get('output_name_meta') }}",
            "-outputNameFault",  "{{ params.get('output_name_fault') }}",
            "-numberOfEmittedFiles", "{{ params.get('numberOfEmittedFiles') }}",
            "-cacheRootDir",     "{{ params.get('cache_location') }}",
            "-lockManagerFactoryClassName", "{{ params.get('lockManagerFactoryClassName') }}",
            "-numberOfEmittedFilesInCache", "{{ params.get('numberOfEmittedFilesInCache') }}",
            # identify_by_checksum chain: return records already present in the cache
            # boolean options (JCommander arity 0) -- must use the '=' separator, otherwise the
            # flag is consumed on its own and the bare true/false token becomes an unknown
            # "main parameter"
            "-returnAlreadyExtractedMeta=true",
            "-activeMetadataExtraction=false",
            "-outputReportRootPath",       report_root,
            "-cacheReportRelativePath",    "{{ params.get('cache_report_relative_path') }}",
            "-extractionReportRelativePath", "import_metadataextraction",
            "-reportPropertiesPrefix",     "{{ params.get('cache_report_properties_prefix') }}",
            "-excludedIds",                "{{ params.get('excluded_ids') }}",
            "-logFaultProcessingTimeThresholdSecs",      "{{ params.get('log_fault_processing_time_threshold_secs') }}",
            "-interruptProcessingTimeThresholdSecs",     "{{ params.get('interrupt_processing_time_threshold_secs') }}",
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
    preprocessing >> metadataextraction_cached
    metadataextraction_cached >> [postprocessing_meta, postprocessing_fault]
    [postprocessing_meta, postprocessing_fault] >> end


metadataextraction_cache_identify_by_checksum()
