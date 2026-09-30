import os
import sys
from datetime import timedelta

from airflow.decorators import dag
from airflow.hooks.base import BaseHook
from airflow.models.param import Param

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
    dag_id="crossref_mute_citations_parsing_and_export",
    dag_display_name="Parse Crossref mute citations and export as OAF entities and relations",
    default_args=default_args,
    params={
        "JAR_METADATAEXTRACTION": Param(
            default="https://maven.ceon.pl/artifactory/iis-snapshots/eu/dnetlib/iis/iis-wf-metadataextraction/1.3.0-SNAPSHOT/iis-wf-metadataextraction-1.3.0-20260918.161323-16-uber.jar",
            type='string',
            description="iis-wf-metadataextraction uber jar containing JsonReferenceParserJob"
        ),
        "JAR_EXPORT": Param(
            default="https://maven.ceon.pl/artifactory/iis-snapshots/eu/dnetlib/iis/iis-wf-export-actionmanager/1.3.0-SNAPSHOT/iis-wf-export-actionmanager-1.3.0-20260930.093533-15-uber.jar",
            type='string',
            description="iis-wf-export-actionmanager uber jar containing CrossrefExporterJob"
        ),
        "SPARK_IMAGE": Param(
            "docker-registry.openaire.eu/kubernetes_devel/spark:4.1.2",
            type='string',
            description="Spark Docker image"),
        "HADOOP_USER_NAME": Param(
            "marek.horst",
            type='string',
            description=""),

        # --- I/O paths ---

        "inputJsonPath": Param(
            default="hdfs://nameservice1/user/marek.horst/crossrefMuteCitations/medium",
            type="string",
            description="Input HDFS path with gzip-compressed JSON packages "
                        "(one JSON record per line with id and ref fields)",
        ),
        "outputAvroPath": Param(
            default="hdfs://nameservice1/tmp/marek.horst/crossref_mute_citations/output_avro",
            type="string",
            description="Output Avro path for parsed ExtractedDocumentMetadata records",
        ),
        "outputEntityPath": Param(
            default="hdfs://nameservice1/tmp/marek.horst/crossref_mute_citations/output_entities",
            type="string",
            description="Output path for exported AtomicAction<Publication> sequence files",
        ),
        "outputRelationPath": Param(
            default="hdfs://nameservice1/tmp/marek.horst/crossref_mute_citations/output_relations",
            type="string",
            description="Output path for exported AtomicAction<Relation> sequence files",
        ),
        "outputReportPath": Param(
            default="hdfs://nameservice1/tmp/marek.horst/crossref_mute_citations/reports",
            type="string",
            description="Output Avro path for execution reports "
                        "(eu.dnetlib.iis.common.schemas.ReportEntry)",
        ),
        "outputFaultPath": Param(
            default="hdfs://nameservice1/tmp/marek.horst/crossref_mute_citations/output_fault",
            type="string",
            description="Output Avro path for faults (eu.dnetlib.iis.audit.schemas.Fault) "
                        "reported for the documents which could not be processed",
        ),

        # --- Cache settings ---
        "cacheRootDir": Param(
            default="hdfs://nameservice1/tmp/marek.horst/crossref_mute_citations/cache",
            type="string",
            description="Reference parsing cache root directory. Parsed documents are cached and "
                        "identified with ExtractedDocumentMetadata#id, so the job can be rerun "
                        "incrementally and only the documents which are not cached yet are parsed",
        ),
        "lockManagerFactoryClassName": Param(
            default="eu.dnetlib.iis.common.lock.HadoopFsLockManagerFactory",
            type="string",
            description="Lock manager factory class name for synchronizing access to the cache directory. "
                        "HadoopFsLockManagerFactory uses a sentinel file on the target storage (HDFS/S3/Ceph) "
                        "and requires no external coordination service. "
                        "Use eu.dnetlib.iis.common.lock.ZookeeperLockManagerFactory for legacy Hadoop HA clusters.",
        ),
        "numberOfEmittedFiles": Param(
            default="1000",
            type="string",
            description="Number of files created by the parse task at output and in cache",
        ),

        # --- Algorithm parameters ---
        "extractedBy": Param(
            default="crossrefBibrefParser",
            type="string",
            description="Value to set in ExtractedDocumentMetadata#extractedBy field",
        ),
        "referenceParser": Param(
            default="grobid",
            type="string",
            description="Reference text parser to use: 'cermine' (default) or 'grobid'",
        ),
        "grobidServerUrl": Param(
            default="http://grobid.default:8070",
            type="string",
            description="Grobid server location, required when referenceParser is set to 'grobid'",
        ),
        "grobidConnectionTimeout": Param(
            default="30000",
            type="string",
            description="Grobid connection timeout in ms",
        ),
        "grobidReadTimeout": Param(
            default="60000",
            type="string",
            description="Grobid read timeout in ms",
        ),

        # --- Algorithm parameters (Grobid batching) ---
        "grobidBatchSize": Param(
            default=32,
            type="integer",
            description="Number of citations sent to Grobid per /api/processCitationList request",
        ),

        # --- Spark tuning ---
        "sparkDriverMemory": Param(
            default="8g",
            type="string",
            description="Memory for the Spark driver",
        ),
        "sparkExecutorMemory": Param(
            default="5g",
            type="string",
            description="Memory per Spark executor",
        ),
        "sparkExecutorMemoryOverhead": Param(
            default="2048",
            type="string",
            description="Off-heap memory overhead per Spark executor in MiB. Overrides the very "
                        "large value predefined for the environment; ~2 GiB is comfortable for "
                        "the 10g executor heap of this JVM-only job",
        ),
        "sparkExecutorInstances": Param(
            default=8,
            type="integer",
            description="Number of Spark executors used by both tasks "
                        "(for the parse task the total in-flight Grobid requests ≈ executors × cores)",
        ),
        "sparkExecutorCores": Param(
            default=2,
            type="integer",
            description="CPU cores per Spark executor",
        ),
        "sparkDynamicAllocation": Param(
            default=False,
            type="boolean",
            description="Enable Spark dynamic allocation. Recommended False: it keeps the Grobid "
                        "concurrency of the parse task predictable and keeps the executors of the "
                        "export task alive between its actions, so the payloads it materializes are "
                        "not lost (and the export is not recomputed) for every action",
        ),
    },
    tags=["openaire", "iis", "crossref", "mutecitation"],
    schedule=None
)
def mute_citations_parsing_and_export():
    # ---------------------------------------------------------------------------
    # Step 1: parse JSON reference records into ExtractedDocumentMetadata Avro
    # ---------------------------------------------------------------------------
    parse_task = generate_spark_operator(
        task_id="parse_json_references",
        task_display_name="Parse JSON references into ExtractedDocumentMetadata",
        jar="{{ params.get('JAR_METADATAEXTRACTION') }}",
        image="{{ params.get('SPARK_IMAGE') }}",
        main_class="eu.dnetlib.iis.wf.metadataextraction.crossref.JsonReferenceParserJob",
        arguments=[
            "-inputPath",       "{{ params.get('inputJsonPath') }}",
            "-outputPath",      "{{ params.get('outputAvroPath') }}",
            "-outputFaultPath", "{{ params.get('outputFaultPath') }}",
            "-outputReportPath","{{ params.get('outputReportPath') }}/parse",
            "-cacheRootDir",    "{{ params.get('cacheRootDir') }}",
            "-lockManagerFactoryClassName", "{{ params.get('lockManagerFactoryClassName') }}",
            "-numberOfEmittedFiles", "{{ params.get('numberOfEmittedFiles') }}",
            "-extractedBy",     "{{ params.get('extractedBy') }}",
            "-referenceParser", "{{ params.get('referenceParser') }}",
            "-grobidServerUrl", "{{ params.get('grobidServerUrl') }}",
            "-grobidConnectionTimeout", "{{ params.get('grobidConnectionTimeout') }}",
            "-grobidReadTimeout", "{{ params.get('grobidReadTimeout') }}",
            "-grobidBatchSize", "{{ params.get('grobidBatchSize') }}",
        ],
        # SparkApplication exposes dynamic allocation as a first-class spec field that
        # overrides sparkConf entries; emit it explicitly so the operator's defaulting
        # webhook (enabled=true, maxExecutors=14) does not win over our settings.
        dynamic_allocation={
            "enabled": "{{ 'true' if params.get('sparkDynamicAllocation') else 'false' }}",
            "minExecutors": "1",
            "maxExecutors": "{{ params.get('sparkExecutorInstances') }}",
            "shuffleTrackingEnabled": "{{ 'true' if params.get('sparkDynamicAllocation') else 'false' }}",
        },
        spark_extra_conf={
            "spark.driver.memory":   "{{ params.get('sparkDriverMemory') }}",
            "spark.executor.memory": "{{ params.get('sparkExecutorMemory') }}",
            # the environment predefines a very large executor memory overhead which would
            # waste cluster memory, so it is overridden with an explicit, tunable value
            "spark.executor.memoryOverhead": "{{ params.get('sparkExecutorMemoryOverhead') }}",
            # predictable concurrency to Grobid: fixed executor count/cores and
            # dynamic allocation off, so in-flight requests ≈ executors × cores
            "spark.executor.instances": "{{ params.get('sparkExecutorInstances') }}",
            "spark.executor.cores":     "{{ params.get('sparkExecutorCores') }}",
            "spark.dynamicAllocation.enabled": "{{ 'true' if params.get('sparkDynamicAllocation') else 'false' }}",
            "spark.driverEnv.HADOOP_USER_NAME":          "{{ params.get('HADOOP_USER_NAME') }}",
            "spark.executorEnv.HADOOP_USER_NAME":        "{{ params.get('HADOOP_USER_NAME') }}",
            "spark.driverEnv.SPARK_USER":                "{{ params.get('HADOOP_USER_NAME') }}",
            "spark.executorEnv.SPARK_USER":              "{{ params.get('HADOOP_USER_NAME') }}",
            "spark.kubernetes.driverEnv.HADOOP_USER_NAME":   "{{ params.get('HADOOP_USER_NAME') }}",
            "spark.kubernetes.executorEnv.HADOOP_USER_NAME": "{{ params.get('HADOOP_USER_NAME') }}",
        }
    )

    # ---------------------------------------------------------------------------
    # Step 2: export ExtractedDocumentMetadata records as OAF entities and
    #          relations (AtomicAction<Publication> + AtomicAction<Relation>)
    # ---------------------------------------------------------------------------
    export_task = generate_spark_operator(
        task_id="export_entities_and_relations",
        task_display_name="Export ExtractedDocumentMetadata as OAF entities and relations",
        jar="{{ params.get('JAR_EXPORT') }}",
        image="{{ params.get('SPARK_IMAGE') }}",
        main_class="eu.dnetlib.iis.wf.export.actionmanager.entity.crossref.CrossrefExporterJob",
        arguments=[
            "-inputPath",         "{{ params.get('outputAvroPath') }}",
            "-outputEntityPath",  "{{ params.get('outputEntityPath') }}",
            "-outputRelationPath", "{{ params.get('outputRelationPath') }}",
            "-outputReportPath",  "{{ params.get('outputReportPath') }}/export",
        ],
        # the export job materializes the serialized payloads once and feeds both outputs and the
        # report counters from that materialization: executors must stay around between those
        # actions, otherwise the cached payloads are lost and the whole export is recomputed for
        # every action (the operator's defaulting webhook would enable dynamic allocation)
        dynamic_allocation={
            "enabled": "{{ 'true' if params.get('sparkDynamicAllocation') else 'false' }}",
            "minExecutors": "1",
            "maxExecutors": "{{ params.get('sparkExecutorInstances') }}",
            "shuffleTrackingEnabled": "{{ 'true' if params.get('sparkDynamicAllocation') else 'false' }}",
        },
        spark_extra_conf={
            "spark.driver.memory":   "{{ params.get('sparkDriverMemory') }}",
            "spark.executor.memory": "{{ params.get('sparkExecutorMemory') }}",
            "spark.executor.memoryOverhead": "{{ params.get('sparkExecutorMemoryOverhead') }}",
            "spark.executor.instances": "{{ params.get('sparkExecutorInstances') }}",
            "spark.executor.cores":     "{{ params.get('sparkExecutorCores') }}",
            "spark.dynamicAllocation.enabled": "{{ 'true' if params.get('sparkDynamicAllocation') else 'false' }}",
            "spark.driverEnv.HADOOP_USER_NAME":          "{{ params.get('HADOOP_USER_NAME') }}",
            "spark.executorEnv.HADOOP_USER_NAME":        "{{ params.get('HADOOP_USER_NAME') }}",
            "spark.driverEnv.SPARK_USER":                "{{ params.get('HADOOP_USER_NAME') }}",
            "spark.executorEnv.SPARK_USER":              "{{ params.get('HADOOP_USER_NAME') }}",
            "spark.kubernetes.driverEnv.HADOOP_USER_NAME":   "{{ params.get('HADOOP_USER_NAME') }}",
            "spark.kubernetes.executorEnv.HADOOP_USER_NAME": "{{ params.get('HADOOP_USER_NAME') }}",
        }
    )

    # ---------------------------------------------------------------------------
    # Wire: parse output feeds into export input
    # ---------------------------------------------------------------------------
    parse_task >> export_task


mute_citations_parsing_and_export()
