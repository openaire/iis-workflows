"""
Uber export DAG — Airflow port of the primary export Oozie workflow.

Ports the main exporter workflow defined in:
    iis/iis-wf/iis-wf-primary/src/main/resources/eu/dnetlib/iis/wf/primary/export/oozie_app/workflow.xml
onto Airflow/Kubernetes.

Original Oozie graph (sub-workflows referenced via `import.txt`):

    export_actionmanager_sequencefile ─ (fork) ─ decision_software_exporter
                                              └─ decision_citation_relation_exporter
         └ export_join ─ primary_export_push_reports ─ distcp_output ─ end

Airflow mapping
---------------
  * export_actionmanager_sequencefile  → triggers the already-ported DAG
        `spark_sequencefile_exporter` (sequencefile-exporter-on-k8s) via
        TriggerDagRunOperator and waits for its completion.
  * software and citation-relation exporters are Spark jobs declared inline
        here (they are not reused elsewhere); patent relations are exported by
        the sequencefile exporter (input_document_to_patent).
  * primary_export_push_reports        → KubernetesPodOperator running
        `ProcessWrapper` around `eu.dnetlib.iis.wf.report.pushgateway.process.PushMetricsProcess`
        (same generic ProcessWrapper pattern as spark_referenceextraction_generic_builder_dag.py).
  * distcp_output                      → Hadoop distcp launched via spark-submit
        using a helper/entry main class (default `org.apache.hadoop.tools.DistCp`).

A single uber JAR (`iis-wf-primary`) is referenced by every Spark/Java task, so
the class of each exporter (SequenceFileExporterJob, SoftwareExporterJob,
CitationRelationExporterJob, PushMetricsProcess, hadoop tools) is resolved from
that one shaded artifact.
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

# Ensure it's in sys.path
if package_dir not in sys.path:
    sys.path.append(package_dir)
if parent_dir not in sys.path:
    sys.path.append(parent_dir)

import dag_utils
from spark_configurator import generate_spark_operator

EXECUTION_TIMEOUT = int(os.getenv("EXECUTION_TIMEOUT", 6))

default_args = {
    "execution_timeout": timedelta(days=EXECUTION_TIMEOUT),
    "retries": int(os.getenv("DEFAULT_TASK_RETRIES", 1)),
    "retry_delay": timedelta(seconds=int(os.getenv("DEFAULT_RETRY_DELAY_SECONDS", 60))),
}

# --------------------------------------------------------------------------- #
# Job entry-point classes (all resolved from the single iis-wf-primary uber JAR)
# --------------------------------------------------------------------------- #
_SEQUENCEFILE_DAG_ID = "spark_sequencefile_exporter"

_SOFTWARE_EXPORTER_CLASS = (
    "eu.dnetlib.iis.wf.export.actionmanager.entity.software.SoftwareExporterJob"
)
_CITATION_RELATION_EXPORTER_CLASS = (
    "eu.dnetlib.iis.wf.export.actionmanager.relation.citation.CitationRelationExporterJob"
)

# --------------------------------------------------------------------------- #
# Undefined sentinel used to skip an optional export
# --------------------------------------------------------------------------- #
_UNDEFINED = "$UNDEFINED$"


# --------------------------------------------------------------------------- #
# Branching helpers (equivalent of the Oozie <decision> nodes)
# --------------------------------------------------------------------------- #
def _decision_software_exporter(params):
    """software exporter runs only when flag is set AND input path was defined."""
    active = params.get("active_export_software")
    input_path = params.get("input_document_to_software_url")
    if str(active).lower() == "true" and input_path and input_path != _UNDEFINED:
        return ["software_exporter"]
    return ["export_join"]


def _decision_citation_relation_exporter(params):
    """citation relation exporter runs when input_citations was defined."""
    input_path = params.get("input_citations")
    if input_path and input_path != _UNDEFINED:
        return ["citation_relation_exporter"]
    return ["export_join"]


def _decision_distcp(params):
    """distcp runs only when a remote output location was provided."""
    remote = params.get("output_remote_location")
    if remote and remote != _UNDEFINED:
        return ["distcp_output"]
    return ["end"]


@dag(
    dag_id="spark_primary_export",
    dag_display_name="Exports all inferences/entities as AtomicAction sequence files (uber export)",
    default_args=default_args,
    params={
        # ------------------------------------------------------------------ #
        #  Single uber artifact + runtime image                               #
        # ------------------------------------------------------------------ #
        "JAR": Param(
            default="https://maven.ceon.pl/artifactory/iis-snapshots/eu/dnetlib/iis/iis-wf-primary/1.3.0-SNAPSHOT/iis-wf-primary-1.3.0-20260903.132346-1-uber.jar",
            type="string",
            description="Uber (shaded) JAR of the iis-wf-primary module. "
                        "Contains every exporter class (sequencefile, software, "
                        "citation relation) plus the report PushMetricsProcess and hadoop tools.",
        ),
        "SPARK_IMAGE": Param(
            "docker-registry.openaire.eu/kubernetes_devel/spark:4.1.2",
            type="string",
            description="Spark Docker image used by every Spark task",
        ),
        "HADOOP_USER_NAME": Param(
            "marek.horst",
            type="string",
            description="Hadoop/HDFS user name propagated to driver, executor and utility pods",
        ),

        # ------------------------------------------------------------------ #
        #  Output roots                                                       #
        # ------------------------------------------------------------------ #
        # Note: mirrors Oozie `${workingDir}/output`; every exporter writes into it and
        # the final distcp copies the whole directory to the remote location.
        "output": Param(
            default="hdfs://iis-cdh5-test-m2.ocean.icm.edu.pl:8020/tmp/marek.horst/primary_export/output",
            type="string",
            description="Root output HDFS directory. Each export action writes to a dedicated "
                        "subdirectory under this path.",
        ),
        "output_report_root_path": Param(
            default="hdfs://iis-cdh5-test-m2.ocean.icm.edu.pl:8020/tmp/marek.horst/primary_export/reports",
            type="string",
            description="Base HDFS directory where per-exporter reports are stored and from which "
                        "metrics are pushed to the pushgateway.",
        ),
        "output_remote_location": Param(
            default="$UNDEFINED$",
            type="string",
            description="Optional remote HDFS location where the whole {output} directory is "
                        "distcped as sequence files. Leave as $UNDEFINED$ to skip the distcp step.",
        ),
        "output_remote_distcp_memory_mb": Param(
            default="6144",
            type="string",
            description="Map task memory (MB) used by the distcp job "
                        "(oozie: output_remote_distcp_memory_mb).",
        ),

        # ------------------------------------------------------------------ #
        #  Inputs (document → inference datastores)                           #
        #  Forwarded to the spark_sequencefile_exporter DAG.                  #
        # ------------------------------------------------------------------ #
        "input_document_metadata": Param(
            default=_UNDEFINED,
            type="string",
            description="ExtractedDocumentMetadataMergedWithOriginal avro records. Required for "
                        "generating alternative software titles (software exporter).",
        ),
        "input_document_to_project": Param(
            default=_UNDEFINED, type="string",
            description="Avro datastore with DocumentToProject inferences. $UNDEFINED$ → skipped.",
        ),
        "input_document_to_dataset": Param(
            default=_UNDEFINED, type="string",
            description="Avro datastore with DocumentToDataSet inferences. $UNDEFINED$ → skipped.",
        ),
        "input_document_to_research_initiatives": Param(
            default=_UNDEFINED, type="string",
            description="Avro datastore with research-initiative concept inferences. $UNDEFINED$ → skipped.",
        ),
        "input_document_to_community": Param(
            default=_UNDEFINED, type="string",
            description="Avro datastore with community concept inferences. $UNDEFINED$ → skipped.",
        ),
        "input_document_to_pdb": Param(
            default=_UNDEFINED, type="string",
            description="Avro datastore with protein-data-bank concept inferences. $UNDEFINED$ → skipped.",
        ),
        "input_document_to_covid19": Param(
            default=_UNDEFINED, type="string",
            description="Avro datastore with COVID-19 concept inferences. $UNDEFINED$ → skipped.",
        ),
        "input_document_to_service": Param(
            default=_UNDEFINED, type="string",
            description="Avro datastore with DocumentToService inferences. $UNDEFINED$ → skipped.",
        ),
        "input_document_to_document_classes": Param(
            default=_UNDEFINED, type="string",
            description="Avro datastore with DocumentToDocumentClasses inferences. $UNDEFINED$ → skipped.",
        ),
        "input_citations": Param(
            default=_UNDEFINED, type="string",
            description="Avro datastore with Citations inferences. $UNDEFINED$ → skipped.",
        ),
        "input_document_similarity": Param(
            default=_UNDEFINED, type="string",
            description="Avro datastore with DocumentSimilarity inferences. $UNDEFINED$ → skipped.",
        ),
        "input_matched_doc_organizations": Param(
            default=_UNDEFINED, type="string",
            description="Avro datastore with MatchedOrganizationWithProvenance inferences. $UNDEFINED$ → skipped.",
        ),
        # entity/relation specific inputs (not part of the sequencefile exporter)
        "input_document_to_software_url": Param(
            default=_UNDEFINED, type="string",
            description="Avro datastore with DocumentToSoftwareUrlWithMeta inferences "
                        "(software exporter). $UNDEFINED$ → skipped.",
        ),
        "input_document_to_patent": Param(
            default=_UNDEFINED, type="string",
            description="Avro datastore with DocumentToPatent inferences; exported as patent "
                        "relations by the sequencefile exporter. $UNDEFINED$ → skipped.",
        ),

        # ------------------------------------------------------------------ #
        #  Entity exporting modes                                             #
        # ------------------------------------------------------------------ #
        "active_export_software": Param(
            default="false", type="string",
            description="Flag indicating software entities should be exported.",
        ),

        # ------------------------------------------------------------------ #
        #  Action-set identifiers                                             #
        # ------------------------------------------------------------------ #
        "action_set_id_document_similarities_standard": Param(default="actionset-id", type="string"),
        "action_set_id_matched_doc_organizations": Param(default="actionset-id", type="string"),
        "action_set_id_document_classes": Param(default="actionset-id", type="string"),
        "action_set_id_document_referencedProjects": Param(default="actionset-id", type="string"),
        "action_set_id_document_referencedDatasets": Param(default="actionset-id", type="string"),
        "action_set_id_document_referencedDocuments": Param(default="actionset-id", type="string"),
        "action_set_id_document_eoscServices": Param(default="actionset-id", type="string"),
        "action_set_id_document_research_initiative": Param(default="actionset-id", type="string"),
        "action_set_id_document_community": Param(default="actionset-id", type="string"),
        "action_set_id_document_pdb": Param(default="actionset-id", type="string"),
        "action_set_id_document_covid19": Param(default="actionset-id", type="string"),
        "action_set_id_document_patent": Param(default="actionset-id", type="string"),
        "action_set_id_document_software_url": Param(default="actionset-id", type="string"),
        "action_set_id_entity_software": Param(default="actionset-id", type="string"),
        "action_set_id_citation_relations": Param(default="actionset-id", type="string"),

        # ------------------------------------------------------------------ #
        #  Trust level thresholds                                             #
        # ------------------------------------------------------------------ #
        "trust_level_threshold": Param(
            default=_UNDEFINED, type="string",
            description="Default trust level threshold of exported data. $UNDEFINED$ → disabled.",
        ),
        "trust_level_threshold_document_classes": Param(default=_UNDEFINED, type="string"),
        "trust_level_threshold_document_referencedProjects": Param(default=_UNDEFINED, type="string"),
        "trust_level_threshold_document_referencedDatasets": Param(default=_UNDEFINED, type="string"),
        "trust_level_threshold_document_eoscServices": Param(default=_UNDEFINED, type="string"),
        "trust_level_threshold_document_referencedDocuments": Param(default=_UNDEFINED, type="string"),
        "trust_level_threshold_document_pdb": Param(default=_UNDEFINED, type="string"),
        "trust_level_threshold_document_software_url": Param(default=_UNDEFINED, type="string"),
        "trust_level_threshold_matched_doc_organizations": Param(default=_UNDEFINED, type="string"),
        "trust_level_threshold_document_patent": Param(default=_UNDEFINED, type="string"),

        # ------------------------------------------------------------------ #
        #  Export algorithm parameters                                        #
        # ------------------------------------------------------------------ #
        "collectedfrom_key": Param(
            default="10|infrastruct_::f66f1bd369679b5b077dcdf006089556",
            type="string",
            description="Datasource identifier stored in Relation#collectedfrom[].key",
        ),
        "documentssimilarity_threshold": Param(
            default="0.7", type="string",
            description="Similarity score threshold below which similarity export is omitted.",
        ),
        "referenceextraction_pdb_url_root": Param(
            default="http://www.rcsb.org/pdb/explore/explore.do?structureId=",
            type="string",
            description="PDB URL root concatenated with pdb identifier to form the final URL.",
        ),

        # ------------------------------------------------------------------ #
        #  Output-file control (sequencefile exporter)                        #
        # ------------------------------------------------------------------ #
        "numberOfOutputFiles": Param(
            default="1", type="string",
            description="Number of output sequence-file parts for exporters that handle a large "
                        "number of small input files. 0 → preserve natural partition count.",
        ),

        # ------------------------------------------------------------------ #
        #  Report pushgateway                                                 #
        # ------------------------------------------------------------------ #
        "metric_pusher_address": Param(
            default="prometheus.openaire.eu:9091",
            type="string",
            description="Pushgateway service location (host:port) receiving the prometheus metrics.",
        ),

        # ------------------------------------------------------------------ #
        #  Distcp                                                             #
        # ------------------------------------------------------------------ #
        "DISTCP_MAIN_CLASS": Param(
            default="org.apache.hadoop.tools.DistCp",
            type="string",
            description="Entry/helper main class used to run the Hadoop distcp tool via spark-submit. "
                        "Defaults to the distcp tool itself. Override with "
                        "eu.dnetlib.dhp.oozie.RunJavaSparkJob (or any wrapper whose main() forwards "
                        "distcp-style args) when the DHP wrapper must bootstrap the driver first.",
        ),

        # ------------------------------------------------------------------ #
        #  Spark resource tuning                                              #
        # ------------------------------------------------------------------ #
        "sparkDriverMemory": Param(default="10g", type="string"),
        "sparkExecutorMemory": Param(default="10g", type="string"),

        # ------------------------------------------------------------------ #
        #  Utility pod (push_reports) HDFS access                             #
        # ------------------------------------------------------------------ #
        "HDFS_NAMENODE": Param(
            default="hdfs://iis-cdh5-test-m2.ocean.icm.edu.pl:8020",
            type="string",
            description="HDFS NameNode URI used to generate a minimal core-site.xml when the "
                        "utility pod has no Hadoop config mounted (fs.defaultFS).",
        ),
        "HADOOP_CONF_DIR": Param(
            default="/opt/hadoop/etc/hadoop",
            type="string",
            description="Directory holding core-site.xml/hdfs-site.xml mounted into the utility pod. "
                        "Used verbatim when it contains a core-site.xml (HA nameservices); otherwise a "
                        "minimal core-site.xml is generated from HDFS_NAMENODE.",
        ),
    },
    tags=["openaire", "iis", "export", "actionmanager", "primary"],
    schedule=None,
)
def primary_export():
    """Uber exporter: sequencefile → software/citation → push reports → distcp."""

    hadoop_user_conf = {
        "spark.driverEnv.HADOOP_USER_NAME":              "{{ params.get('HADOOP_USER_NAME') }}",
        "spark.executorEnv.HADOOP_USER_NAME":            "{{ params.get('HADOOP_USER_NAME') }}",
        "spark.driverEnv.SPARK_USER":                    "{{ params.get('HADOOP_USER_NAME') }}",
        "spark.executorEnv.SPARK_USER":                  "{{ params.get('HADOOP_USER_NAME') }}",
        "spark.kubernetes.driverEnv.HADOOP_USER_NAME":   "{{ params.get('HADOOP_USER_NAME') }}",
        "spark.kubernetes.executorEnv.HADOOP_USER_NAME": "{{ params.get('HADOOP_USER_NAME') }}",
    }

    export_spark_conf = {
        "spark.driver.memory":   "{{ params.get('sparkDriverMemory') }}",
        "spark.executor.memory": "{{ params.get('sparkExecutorMemory') }}",
        **hadoop_user_conf,
    }

    # ---------------------------------------------------------------------- #
    # 1. export_actionmanager_sequencefile                                    #
    #    Triggers the already-ported DAG and waits for its completion.        #
    # ---------------------------------------------------------------------- #
    export_sequencefile = TriggerDagRunOperator(
        task_id="export_actionmanager_sequencefile",
        task_display_name="Export document relations as sequence files (spark_sequencefile_exporter)",
        trigger_dag_id=_SEQUENCEFILE_DAG_ID,
        conf={
            # single uber JAR for every spark task inside the child DAG
            "JAR": "{{ params.get('JAR') }}",
            "SPARK_IMAGE": "{{ params.get('SPARK_IMAGE') }}",
            "HADOOP_USER_NAME": "{{ params.get('HADOOP_USER_NAME') }}",
            # inputs
            "input_document_to_project": "{{ params.get('input_document_to_project') }}",
            "input_document_to_dataset": "{{ params.get('input_document_to_dataset') }}",
            "input_document_to_patent": "{{ params.get('input_document_to_patent') }}",
            "input_document_to_service": "{{ params.get('input_document_to_service') }}",
            "input_document_to_research_initiatives": "{{ params.get('input_document_to_research_initiatives') }}",
            "input_document_to_community": "{{ params.get('input_document_to_community') }}",
            "input_document_to_pdb": "{{ params.get('input_document_to_pdb') }}",
            "input_document_to_covid19": "{{ params.get('input_document_to_covid19') }}",
            "input_document_to_document_classes": "{{ params.get('input_document_to_document_classes') }}",
            "input_citations": "{{ params.get('input_citations') }}",
            "input_document_similarity": "{{ params.get('input_document_similarity') }}",
            "input_matched_doc_organizations": "{{ params.get('input_matched_doc_organizations') }}",
            # output
            "output": "{{ params.get('output') }}",
            # action-set identifiers
            "action_set_id_document_similarities_standard": "{{ params.get('action_set_id_document_similarities_standard') }}",
            "action_set_id_matched_doc_organizations": "{{ params.get('action_set_id_matched_doc_organizations') }}",
            "action_set_id_document_classes": "{{ params.get('action_set_id_document_classes') }}",
            "action_set_id_document_referencedProjects": "{{ params.get('action_set_id_document_referencedProjects') }}",
            "action_set_id_document_referencedDatasets": "{{ params.get('action_set_id_document_referencedDatasets') }}",
            "action_set_id_document_referencedDocuments": "{{ params.get('action_set_id_document_referencedDocuments') }}",
            "action_set_id_document_eoscServices": "{{ params.get('action_set_id_document_eoscServices') }}",
            "action_set_id_document_research_initiative": "{{ params.get('action_set_id_document_research_initiative') }}",
            "action_set_id_document_community": "{{ params.get('action_set_id_document_community') }}",
            "action_set_id_document_pdb": "{{ params.get('action_set_id_document_pdb') }}",
            "action_set_id_document_covid19": "{{ params.get('action_set_id_document_covid19') }}",
            "action_set_id_document_patent": "{{ params.get('action_set_id_document_patent') }}",
            # trust level thresholds
            "trust_level_threshold": "{{ params.get('trust_level_threshold') }}",
            "trust_level_threshold_document_classes": "{{ params.get('trust_level_threshold_document_classes') }}",
            "trust_level_threshold_document_referencedProjects": "{{ params.get('trust_level_threshold_document_referencedProjects') }}",
            "trust_level_threshold_document_referencedDatasets": "{{ params.get('trust_level_threshold_document_referencedDatasets') }}",
            "trust_level_threshold_document_referencedDocuments": "{{ params.get('trust_level_threshold_document_referencedDocuments') }}",
            "trust_level_threshold_document_patent": "{{ params.get('trust_level_threshold_document_patent') }}",
            "trust_level_threshold_document_pdb": "{{ params.get('trust_level_threshold_document_pdb') }}",
            "trust_level_threshold_matched_doc_organizations": "{{ params.get('trust_level_threshold_matched_doc_organizations') }}",
            # algorithm params
            "collectedfrom_key": "{{ params.get('collectedfrom_key') }}",
            "documentssimilarity_threshold": "{{ params.get('documentssimilarity_threshold') }}",
            "referenceextraction_pdb_url_root": "{{ params.get('referenceextraction_pdb_url_root') }}",
            # output-file control + resources
            "numberOfOutputFiles": "{{ params.get('numberOfOutputFiles') }}",
            "sparkDriverMemory": "{{ params.get('sparkDriverMemory') }}",
            "sparkExecutorMemory": "{{ params.get('sparkExecutorMemory') }}",
        },
        wait_for_completion=True,
        deferrable=False,
        poke_interval=60,
        allowed_states=["success"],
        failed_states=["failed"],
    )

    # ---------------------------------------------------------------------- #
    # Decision nodes                                                          #
    # ---------------------------------------------------------------------- #
    decision_software = BranchPythonOperator(
        task_id="decision_software_exporter",
        python_callable=_decision_software_exporter,
    )
    decision_citation = BranchPythonOperator(
        task_id="decision_citation_relation_exporter",
        python_callable=_decision_citation_relation_exporter,
    )

    # ---------------------------------------------------------------------- #
    # 2. software_exporter (Spark)                                            #
    #    Oozie sub-workflow: export_software                                   #
    # ---------------------------------------------------------------------- #
    software_exporter = generate_spark_operator(
        task_id="software_exporter",
        task_display_name="Export software entities and relations (SoftwareExporterJob)",
        jar="{{ params.get('JAR') }}",
        image="{{ params.get('SPARK_IMAGE') }}",
        main_class=_SOFTWARE_EXPORTER_CLASS,
        arguments=[
            "-inputDocumentToSoftwareUrlPath", "{{ params.get('input_document_to_software_url') }}",
            "-inputDocumentMetadataPath",      "{{ params.get('input_document_metadata') }}",
            "-outputEntityPath",               "{{ params.get('output') }}/entities_software/{{ params.get('action_set_id_entity_software') }}",
            "-outputRelationPath",             "{{ params.get('output') }}/document_software_url/{{ params.get('action_set_id_document_software_url') }}",
            "-outputReportPath",               "{{ params.get('output_report_root_path') }}/export_software",
            "-trustLevelThreshold",            "{{ params.get('trust_level_threshold_document_software_url') }}",
            "-collectedFromKey",               "{{ params.get('collectedfrom_key') }}",
        ],
        spark_extra_conf=export_spark_conf,
    )

    # ---------------------------------------------------------------------- #
    # 3. citation_relation_exporter (Spark)                                   #
    #    Oozie sub-workflow: export_citation_relation                          #
    # ---------------------------------------------------------------------- #
    citation_relation_exporter = generate_spark_operator(
        task_id="citation_relation_exporter",
        task_display_name="Export citation relations (CitationRelationExporterJob)",
        jar="{{ params.get('JAR') }}",
        image="{{ params.get('SPARK_IMAGE') }}",
        main_class=_CITATION_RELATION_EXPORTER_CLASS,
        arguments=[
            "-inputCitationsPath",  "{{ params.get('input_citations') }}",
            "-outputRelationPath",  "{{ params.get('output') }}/relations_citation/{{ params.get('action_set_id_citation_relations') }}",
            "-outputReportPath",    "{{ params.get('output_report_root_path') }}/export_citation_relation",
            "-trustLevelThreshold", "{{ params.get('trust_level_threshold_document_referencedDocuments') }}",
            "-collectedFromKey",    "{{ params.get('collectedfrom_key') }}",
        ],
        spark_extra_conf=export_spark_conf,
    )

    # ---------------------------------------------------------------------- #
    # Join + push reports + distcp                                            #
    # ---------------------------------------------------------------------- #
    export_join = EmptyOperator(
        task_id="export_join",
        task_display_name="Join software/citation exporters",
        trigger_rule="none_failed",
    )

    # 5. primary_export_push_reports — ProcessWrapper around PushMetricsProcess
    push_reports = KubernetesPodOperator(
        task_id="primary_export_push_reports",
        task_display_name="Push execution reports to the pushgateway (PushMetricsProcess)",

        # ---- Pod identity ---- #
        name="push-reports-{{ ds }}-{{ task_instance.try_number }}",
        namespace="spark-jobs",
        kubernetes_conn_id="kubernetes_default",

        # ---- Image ---- #
        image="{{ params.get('SPARK_IMAGE') }}",
        image_pull_policy="Always",

        # ---- Override the Spark entrypoint ---- #
        cmds=["/bin/bash"],
        arguments=["-c", r"""
set -euo pipefail

# ------------------------------------------------------------------ #
#  0.  Basic setup                                                    #
# ------------------------------------------------------------------ #
JAR_URL="{{ params.get('JAR') }}"
JAR_FILENAME=$(basename "$JAR_URL")
TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT

export HADOOP_USER_NAME="{{ params.get('HADOOP_USER_NAME') }}"

# ------------------------------------------------------------------ #
#  1.  Download the uber-JAR                                          #
# ------------------------------------------------------------------ #
echo "[STEP 1] Downloading uber-JAR"
wget -q -O "${TMP_DIR}/${JAR_FILENAME}" "$JAR_URL"

# ------------------------------------------------------------------ #
#  2.  Hadoop configuration                                           #
# ------------------------------------------------------------------ #
echo "[STEP 2] Resolving Hadoop configuration"
HADOOP_CONF_DIR='{{ params.get('HADOOP_CONF_DIR') }}'
if [ -f "${HADOOP_CONF_DIR}/core-site.xml" ]; then
    # Hadoop config is mounted (e.g. via ConfigMap) — reuse it as-is.
    # This is required for HA nameservice URIs (hdfs://nameservice1/...).
    echo "  Using mounted Hadoop config from ${HADOOP_CONF_DIR}"
    CP_CONF="${HADOOP_CONF_DIR}"
else
    # Fall back to a minimal core-site.xml pointing at a direct NameNode.
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

# ------------------------------------------------------------------ #
#  3.  Run ProcessWrapper + PushMetricsProcess                         #
# ------------------------------------------------------------------ #
echo "[STEP 3] Running PushMetricsProcess"
echo "  reports dir:  {{ params.get('output_report_root_path') }}"
echo "  push address: {{ params.get('metric_pusher_address') }}"

cd "${TMP_DIR}"
SPARK_CP="/opt/spark/jars/*"

java \
    -Djava.io.tmpdir="${TMP_DIR}" \
    -cp "${CP_CONF}:${SPARK_CP}:${TMP_DIR}/${JAR_FILENAME}" \
    eu.dnetlib.iis.common.java.ProcessWrapper \
    eu.dnetlib.iis.wf.report.pushgateway.process.PushMetricsProcess \
    -PmetricPusherCreatorClassName=eu.dnetlib.iis.wf.report.pushgateway.process.PushGatewayPusherCreator \
    -PmetricPusherAddress={{ params.get('metric_pusher_address') }} \
    -PreportsDirPath={{ params.get('output_report_root_path') }} \
    -PlabeledMetricsPropertiesFile=eu/dnetlib/iis/wf/report/pushgateway/process/oozie_app/labeled_metrics.properties \
    -PgroupingKey.user={{ params.get('HADOOP_USER_NAME') }}

echo "[DONE] Reports pushed successfully"
        """],

        # ---- Environment ---- #
        env_vars={
            "HADOOP_USER_NAME": "{{ params.get('HADOOP_USER_NAME') }}",
        },

        # ---- Startup timeout ---- #
        startup_timeout_seconds=300,

        # ---- Behaviour ---- #
        get_logs=True,
        is_delete_operator_pod=True,
    )

    # 6. distcp_output — Hadoop distcp launched via spark-submit (helper main class)
    decision_distcp = BranchPythonOperator(
        task_id="decision_distcp",
        python_callable=_decision_distcp,
    )

    distcp_output = generate_spark_operator(
        task_id="distcp_output",
        task_display_name="Distcp export output to the remote location",
        jar="{{ params.get('JAR') }}",
        image="{{ params.get('SPARK_IMAGE') }}",
        # Entry/helper main class running the Hadoop distcp tool. Defaults to the
        # distcp tool itself; set to eu.dnetlib.dhp.oozie.RunJavaSparkJob (or any
        # wrapper whose main() forwards distcp-style args) when a DHP wrapper must
        # bootstrap the driver first.
        main_class="{{ params.get('DISTCP_MAIN_CLASS') }}",
        arguments=[
            "-Dmapreduce.map.memory.mb={{ params.get('output_remote_distcp_memory_mb') }}",
            "-pb",
            "-overwrite",
            "{{ params.get('output') }}",
            "{{ params.get('output_remote_location') }}",
        ],
        spark_extra_conf={
            **export_spark_conf,
            "spark.dynamicAllocation.enabled": "false",
            "spark.dynamicAllocation.minExecutors": "0",
            "spark.dynamicAllocation.maxExecutors": "1",
        },
    )

    end = EmptyOperator(task_id="end", task_display_name="Export finished")

    # ---------------------------------------------------------------------- #
    # Wiring (Oozie: start → sequencefile → fork/join → push_reports → distcp)
    # ---------------------------------------------------------------------- #
    export_sequencefile >> [decision_software, decision_citation]

    decision_software >> [software_exporter, export_join]
    software_exporter >> export_join

    decision_citation >> [citation_relation_exporter, export_join]
    citation_relation_exporter >> export_join

    export_join >> push_reports >> decision_distcp
    decision_distcp >> [distcp_output, end]
    distcp_output >> end


primary_export()
