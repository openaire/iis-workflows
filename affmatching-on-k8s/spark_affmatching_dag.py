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
    dag_id="spark_affmatching",
    dag_display_name="Performs affiliation matching algorithm matching publications with organizations",
    default_args=default_args,
    params={
        "JAR": Param(
            default="https://maven.ceon.pl/artifactory/iis-snapshots/eu/dnetlib/iis/iis-wf-affmatching/1.3.0-SNAPSHOT/iis-wf-affmatching-1.3.0-20260618.150701-1-uber.jar",
            type='string',
            description="affiliation matching shaded jar"
        ),
        "SPARK_IMAGE": Param(
            "docker-registry.openaire.eu/kubernetes_devel/spark:4.1.1",
            type='string',
            description=""),
        "HADOOP_USER_NAME": Param(
            "marek.horst",
            type='string',
            description=""),
        # --- I/O paths (most commonly overridden) ---
        "inputAvroOrgPath": Param(
            default="hdfs://nameservice1/user/dnet.production/iis/working_dirs/primary/primary_import/metadataimport/organization",
            type="string",
            description="Input Avro path with organizations",
        ),
        "inputAvroAffPath": Param(
            # --- default="hdfs://nameservice1/user/dnet.production/iis/working_dirs/primary/primary_import/extracted_document_metadata",
            default="hdfs://nameservice1/tmp/marek.horst/affmatching/input-small",
            type="string",
            description="Input Avro path with document affiliations",
        ),
        "inputAvroDocProjPath": Param(
            default="hdfs://nameservice1/user/dnet.production/iis/working_dirs/primary/primary_import/metadataimport/docproject",
            type="string",
            description="Input Avro path with document to project relations",
        ),
        "inputAvroInferredDocProjPath": Param(
            default="hdfs://nameservice1/user/dnet.production/iis/working_dirs/primary/exported/document_to_project",
            type="string",
            description="Input Avro path with inferred document to project relations",
        ),
        "inputDocProjConfidenceThreshold": Param(
            default="0.5",
            type="string",
            description="Minimal confidence level for document to project relations (leave empty for no limit)",
        ),
        "inputAvroProjOrgPath": Param(
            default="hdfs://nameservice1/user/dnet.production/iis/working_dirs/primary/primary_import/metadataimport/projectorg",
            type="string",
            description="Input Avro path with project to organization relations",
        ),
        "numberOfEmittedFiles": Param(
            default="1000",
            type="string",
            description="Number of output Avro files emitted",
        ),
        "output": Param(
            default="hdfs://iis-cdh5-test-m1.ocean.icm.edu.pl:8020/tmp/k8s-spark/affmatching/output",
            type="string",
            description="Output Avro path for matched organizations",
        ),
        "output_report_root_path": Param(
            default="hdfs://iis-cdh5-test-m1.ocean.icm.edu.pl:8020/tmp/k8s-spark/affmatching/reports",
            type="string",
            description="Output Avro path for final reports",
        ),

        # --- Spark resource tuning ---
        # --- TODO check if sparkExecutorCores (or executor-cores) should be added as a parameter 
        # --- along with other spark-related params from workflow.xml file such as:
        # --- spark.network.timeout=1200s
        # --- spark.executor.heartbeatInterval=1m
        # --- spark.driver.maxResultSize=2g
        # --- spark.shuffle.useOldFetchProtocol=true


        "sparkDriverMemory": Param(
            default="16g",
            type="string",
            description="Memory per driver",
        ),
        "sparkExecutorMemory": Param(
            default="7g",
            type="string",
            description="Memory per executor",
        ),

    },
    tags=["openaire", "iis", "affmatching"],
    schedule=None
)
def affmatching():
        generate_spark_operator(
            task_id="affmatching",
            task_display_name="Perform affiliation matching algorithm",
            jar="{{ params.get('JAR') }}",
            image="{{ params.get('SPARK_IMAGE') }}",
            main_class="eu.dnetlib.iis.wf.affmatching.AffMatchingJob",
            arguments=[
                "-inputAvroOrgPath", "{{ params.get('inputAvroOrgPath') }}",
                "-inputAvroAffPath", "{{ params.get('inputAvroAffPath') }}",
                "-inputAvroDocProjPath", "{{ params.get('inputAvroDocProjPath') }}",
                "-inputAvroInferredDocProjPath", "{{ params.get('inputAvroInferredDocProjPath') }}",
                "-inputDocProjConfidenceThreshold", "{{ params.get('inputDocProjConfidenceThreshold') }}",
                "-inputAvroProjOrgPath", "{{ params.get('inputAvroProjOrgPath') }}",
                "-numberOfEmittedFiles", "{{ params.get('numberOfEmittedFiles') }}",
                "-outputAvroPath", "{{ params.get('output') }}",
                "-outputAvroReportPath", "{{ params.get('output_report_root_path') }}"
            ],
            spark_extra_conf={
                "spark.driver.memory": "{{ params.get('sparkDriverMemory') }}", 
                "spark.executor.memory": "{{ params.get('sparkExecutorMemory') }}",
                "spark.driverEnv.HADOOP_USER_NAME" : "{{ params.get('HADOOP_USER_NAME') }}",
                "spark.executorEnv.HADOOP_USER_NAME" : "{{ params.get('HADOOP_USER_NAME') }}",
                "spark.driverEnv.SPARK_USER" : "{{ params.get('HADOOP_USER_NAME') }}",
                "spark.executorEnv.SPARK_USER" : "{{ params.get('HADOOP_USER_NAME') }}",
                "spark.kubernetes.driverEnv.HADOOP_USER_NAME": "{{ params.get('HADOOP_USER_NAME') }}",
                "spark.kubernetes.executorEnv.HADOOP_USER_NAME": "{{ params.get('HADOOP_USER_NAME') }}"
            }
        )
    


affmatching()
