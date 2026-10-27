import os
import sys
import time
from datetime import timedelta

from airflow.sdk import Param, dag
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

_SEQUENCEFILE_EXPORTER_DAG_ID = "spark_sequencefile_exporter"


@dag(
    dag_id="crossref_citations_parsing_and_export",
    dag_display_name="Parse Crossref citations and export as AtomicAction sequence files",
    default_args=default_args,
    params={
        "JAR_METADATAEXTRACTION": Param(
            default="https://maven.ceon.pl/artifactory/iis-snapshots/eu/dnetlib/iis/iis-wf-metadataextraction/1.3.0-SNAPSHOT/iis-wf-metadataextraction-1.3.0-20261027.131246-17-uber.jar",
            type="string",
            description="iis-wf-metadataextraction uber JAR containing JsonReferenceParserJob",
        ),
        "JAR_TRANSFORMERS": Param(
            default="https://maven.ceon.pl/artifactory/iis-snapshots/eu/dnetlib/iis/iis-wf-transformers/1.3.0-SNAPSHOT/iis-wf-transformers-1.3.0-20261027.131055-18-uber.jar",
            type="string",
            description="iis-wf-transformers uber JAR containing the metadata and citation transformer jobs",
        ),
        "JAR_EXPORT": Param(
            default="https://maven.ceon.pl/artifactory/iis-snapshots/eu/dnetlib/iis/iis-wf-export-actionmanager/1.3.0-SNAPSHOT/iis-wf-export-actionmanager-1.3.0-20261027.162921-17-uber.jar",
            type="string",
            description="iis-wf-export-actionmanager uber JAR passed to the sequence-file exporter DAG",
        ),
        "SPARK_IMAGE": Param(
            "docker-registry.openaire.eu/kubernetes_devel/spark:4.1.2",
            type="string",
            description="Spark Docker image",
        ),
        "HADOOP_USER_NAME": Param(
            "marek.horst",
            type="string",
            description="Hadoop user name used when reading from and writing to HDFS",
        ),
        "inputJsonPath": Param(
            default="hdfs://nameservice1/user/marek.horst/crossrefMuteCitationsBenchmark/micro",
            type="string",
            description="Input HDFS path with Crossref JSON records containing bibliographic references",
        ),
        "inputBaseMetadataPath": Param(
            default="hdfs://nameservice1/tmp/empty-document-metadata",
            type="string",
            description="Empty DocumentMetadata datastore used as the merger's base metadata input",
        ),
        "workingDir": Param(
            default="hdfs://nameservice1/tmp/marek.horst/crossref_citations_benchmark",
            type="string",
            description="HDFS working directory for intermediate datastores, reports, and exported actions",
        ),
        "action_set_id_document_referencedDocuments": Param(
            default=str(int(time.time() * 1000)),
            type="string",
            description="Action-set identifier for citation export, defaulting to the current time in milliseconds",
        ),
        "numberOfEmittedFiles": Param(
            default="1000",
            type="string",
            description="Number of files created by the reference parser at output and in cache",
        ),
        "lockManagerFactoryClassName": Param(
            default="eu.dnetlib.iis.common.lock.HadoopFsLockManagerFactory",
            type="string",
            description="Lock manager factory used to synchronize reference parser cache access",
        ),
        "extractedBy": Param(
            default="crossrefBibrefParser",
            type="string",
            description="Value written to ExtractedDocumentMetadata#extractedBy",
        ),
        "referenceParser": Param(
            default="grobid",
            type="string",
            description="Reference text parser to use: cermine or grobid",
        ),
        "grobidServerUrl": Param(
            default="http://grobid.default:8070",
            type="string",
            description="Grobid server URL when referenceParser is grobid",
        ),
        "grobidConnectionTimeout": Param(default="30000", type="string"),
        "grobidReadTimeout": Param(default="60000", type="string"),
        "grobidBatchSize": Param(default=32, type="integer"),
        "sparkDriverMemory": Param(default="8g", type="string"),
        "sparkExecutorMemory": Param(default="5g", type="string"),
        "sparkExecutorMemoryOverhead": Param(default="2048", type="string"),
        "sparkExecutorInstances": Param(default=8, type="integer"),
        "sparkExecutorCores": Param(default=2, type="integer"),
        "sparkDynamicAllocation": Param(default=False, type="boolean"),
    },
    tags=["openaire", "iis", "crossref", "citations"],
    schedule=None,
)
def crossref_citations_parsing_and_export():
    hadoop_user_conf = {
        "spark.driverEnv.HADOOP_USER_NAME": "{{ params.get('HADOOP_USER_NAME') }}",
        "spark.executorEnv.HADOOP_USER_NAME": "{{ params.get('HADOOP_USER_NAME') }}",
        "spark.driverEnv.SPARK_USER": "{{ params.get('HADOOP_USER_NAME') }}",
        "spark.executorEnv.SPARK_USER": "{{ params.get('HADOOP_USER_NAME') }}",
        "spark.kubernetes.driverEnv.HADOOP_USER_NAME": "{{ params.get('HADOOP_USER_NAME') }}",
        "spark.kubernetes.executorEnv.HADOOP_USER_NAME": "{{ params.get('HADOOP_USER_NAME') }}",
    }

    spark_extra_conf = {
        "spark.driver.memory": "{{ params.get('sparkDriverMemory') }}",
        "spark.executor.memory": "{{ params.get('sparkExecutorMemory') }}",
        "spark.executor.memoryOverhead": "{{ params.get('sparkExecutorMemoryOverhead') }}",
        "spark.executor.instances": "{{ params.get('sparkExecutorInstances') }}",
        "spark.executor.cores": "{{ params.get('sparkExecutorCores') }}",
        "spark.dynamicAllocation.enabled": "{{ 'true' if params.get('sparkDynamicAllocation') else 'false' }}",
        **hadoop_user_conf,
    }
    dynamic_allocation = {
        "enabled": "{{ 'true' if params.get('sparkDynamicAllocation') else 'false' }}",
        "minExecutors": "1",
        "maxExecutors": "{{ params.get('sparkExecutorInstances') }}",
        "shuffleTrackingEnabled": "{{ 'true' if params.get('sparkDynamicAllocation') else 'false' }}",
    }

    parse_references = generate_spark_operator(
        task_id="parse_json_references",
        task_display_name="Parse Crossref JSON references into ExtractedDocumentMetadata",
        jar="{{ params.get('JAR_METADATAEXTRACTION') }}",
        image="{{ params.get('SPARK_IMAGE') }}",
        main_class="eu.dnetlib.iis.wf.metadataextraction.crossref.JsonReferenceParserJob",
        arguments=[
            "-inputPath", "{{ params.get('inputJsonPath') }}",
            "-outputPath", "{{ params.get('workingDir') }}/parsed_metadata",
            "-outputFaultPath", "{{ params.get('workingDir') }}/parser/faults",
            "-outputReportPath", "{{ params.get('workingDir') }}/reports/parser",
            "-cacheRootDir", "{{ params.get('workingDir') }}/cache",
            "-lockManagerFactoryClassName", "{{ params.get('lockManagerFactoryClassName') }}",
            "-numberOfEmittedFiles", "{{ params.get('numberOfEmittedFiles') }}",
            "-extractedBy", "{{ params.get('extractedBy') }}",
            "-referenceParser", "{{ params.get('referenceParser') }}",
            "-grobidServerUrl", "{{ params.get('grobidServerUrl') }}",
            "-grobidConnectionTimeout", "{{ params.get('grobidConnectionTimeout') }}",
            "-grobidReadTimeout", "{{ params.get('grobidReadTimeout') }}",
            "-grobidBatchSize", "{{ params.get('grobidBatchSize') }}",
        ],
        dynamic_allocation=dynamic_allocation.copy(),
        spark_extra_conf=spark_extra_conf.copy(),
    )

    merge_metadata = generate_spark_operator(
        task_id="merge_metadata",
        task_display_name="Merge extracted metadata with empty base metadata",
        jar="{{ params.get('JAR_TRANSFORMERS') }}",
        image="{{ params.get('SPARK_IMAGE') }}",
        main_class="eu.dnetlib.iis.wf.transformers.metadatamerger.MetadataMergerJob",
        arguments=[
            "-inputBaseMetadata", "{{ params.get('inputBaseMetadataPath') }}",
            "-inputExtractedMetadata", "{{ params.get('workingDir') }}/parsed_metadata",
            "-outputMergedMetadata", "{{ params.get('workingDir') }}/merged_metadata",
        ],
        dynamic_allocation=dynamic_allocation.copy(),
        spark_extra_conf=spark_extra_conf.copy(),
    )

    build_citations = generate_spark_operator(
        task_id="build_citations",
        task_display_name="Build Citation records from reference metadata",
        jar="{{ params.get('JAR_TRANSFORMERS') }}",
        image="{{ params.get('SPARK_IMAGE') }}",
        main_class="eu.dnetlib.iis.wf.transformers.common.citations.from.referencemetadata.CitationsFromReferenceMetadataTransformerJob",
        arguments=[
            "-input", "{{ params.get('workingDir') }}/merged_metadata",
            "-output", "{{ params.get('workingDir') }}/citations",
        ],
        dynamic_allocation=dynamic_allocation.copy(),
        spark_extra_conf=spark_extra_conf.copy(),
    )

    group_citations = generate_spark_operator(
        task_id="group_citations",
        task_display_name="Group Citation records by source document",
        jar="{{ params.get('JAR_TRANSFORMERS') }}",
        image="{{ params.get('SPARK_IMAGE') }}",
        main_class="eu.dnetlib.iis.wf.transformers.export.citations.ExportCitationsTransformerJob",
        arguments=[
            "-input", "{{ params.get('workingDir') }}/citations",
            "-output", "{{ params.get('workingDir') }}/exported_citations",
        ],
        dynamic_allocation=dynamic_allocation.copy(),
        spark_extra_conf=spark_extra_conf.copy(),
    )

    export_citations = TriggerDagRunOperator(
        task_id="export_citations_as_atomic_actions",
        task_display_name="Export grouped citations as AtomicAction sequence files",
        trigger_dag_id=_SEQUENCEFILE_EXPORTER_DAG_ID,
        conf={
            "JAR": "{{ params.get('JAR_EXPORT') }}",
            "SPARK_IMAGE": "{{ params.get('SPARK_IMAGE') }}",
            "HADOOP_USER_NAME": "{{ params.get('HADOOP_USER_NAME') }}",
            "input_citations": "{{ params.get('workingDir') }}/exported_citations",
            "output": "{{ params.get('workingDir') }}/atomic_actions",
            "action_set_id_document_referencedDocuments": "{{ params.get('action_set_id_document_referencedDocuments') }}",
            "output_report_root_path": "{{ params.get('workingDir') }}/reports/export",
        },
        wait_for_completion=True,
        deferrable=False,
        poke_interval=60,
        allowed_states=["success"],
        failed_states=["failed"],
    )

    parse_references >> merge_metadata >> build_citations >> group_citations >> export_citations


crossref_citations_parsing_and_export()
