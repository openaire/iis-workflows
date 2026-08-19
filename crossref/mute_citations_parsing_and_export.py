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
            default="https://maven.ceon.pl/artifactory/iis-snapshots/eu/dnetlib/iis/iis-wf-metadataextraction/1.3.0-SNAPSHOT/iis-wf-metadataextraction-1.3.0-20260819.103816-5-uber.jar",
            type='string',
            description="iis-wf-metadataextraction uber jar containing JsonReferenceParserJob"
        ),
        "JAR_EXPORT": Param(
            default="https://maven.ceon.pl/artifactory/iis-snapshots/eu/dnetlib/iis/iis-wf-export-actionmanager/1.3.0-SNAPSHOT/iis-wf-export-actionmanager-1.3.0-20260731.124621-2-uber.jar",
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
            default="hdfs://iis-cdh5-test-m1.ocean.icm.edu.pl:8020/user/marek.horst/crossrefMuteCitations/micro",
            type="string",
            description="Input HDFS path with gzip-compressed JSON packages "
                        "(one JSON record per line with id and ref fields)",
        ),
        "outputAvroPath": Param(
            default="hdfs://iis-cdh5-test-m1.ocean.icm.edu.pl:8020/tmp/marek.horst/crossref_mute_citations/output_avro",
            type="string",
            description="Output Avro path for parsed ExtractedDocumentMetadata records",
        ),
        "outputEntityPath": Param(
            default="hdfs://iis-cdh5-test-m1.ocean.icm.edu.pl:8020/tmp/marek.horst/crossref_mute_citations/output_entities",
            type="string",
            description="Output path for exported AtomicAction<Publication> sequence files",
        ),
        "outputRelationPath": Param(
            default="hdfs://iis-cdh5-test-m1.ocean.icm.edu.pl:8020/tmp/marek.horst/crossref_mute_citations/output_relations",
            type="string",
            description="Output path for exported AtomicAction<Relation> sequence files",
        ),
        "outputReportPath": Param(
            default="hdfs://iis-cdh5-test-m1.ocean.icm.edu.pl:8020/tmp/marek.horst/crossref_mute_citations/reports",
            type="string",
            description="Output Avro path for execution reports "
                        "(eu.dnetlib.iis.common.schemas.ReportEntry)",
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
            default="http://10.19.65.11:8070",
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

        # --- Spark tuning ---
        "sparkDriverMemory": Param(
            default="8g",
            type="string",
            description="Memory for the Spark driver",
        ),
        "sparkExecutorMemory": Param(
            default="10g",
            type="string",
            description="Memory per Spark executor",
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
            "-outputReportPath","{{ params.get('outputReportPath') }}/parse",
            "-extractedBy",     "{{ params.get('extractedBy') }}",
            "-referenceParser", "{{ params.get('referenceParser') }}",
            "-grobidServerUrl", "{{ params.get('grobidServerUrl') }}",
            "-grobidConnectionTimeout", "{{ params.get('grobidConnectionTimeout') }}",
            "-grobidReadTimeout", "{{ params.get('grobidReadTimeout') }}",
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

    # ---------------------------------------------------------------------------
    # Wire: parse output feeds into export input
    # ---------------------------------------------------------------------------
    parse_task >> export_task


mute_citations_parsing_and_export()
