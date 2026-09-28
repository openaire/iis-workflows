"""
Reusable content URL import chain — Airflow port of the Oozie workflow
``eu/dnetlib/iis/wf/importer/content_url/chain/oozie_app``.

This workflow is reused in several IIS contexts (metadata extraction cache builder, primary
import, ...) and therefore has to stay an Airflow DAG (its logic must not be absorbed into a
single Spark job).

Original Oozie graph (sub-workflows referenced via ``import.txt``)::

    import_content_url_core_parquet ─ import_content_url_dedup ─ content-url-dispatcher ─ report
        └ transformers_idreplacer (optional)
        └ transformers_common_existencefilter (optional)

Airflow mapping
---------------
  * ``import_content_url_core_parquet``       → ``HiveBasedDocumentContentUrlImporterJob`` (already Spark)
  * ``transformers_idreplacer``               → ``IdReplacerJob`` (already Spark, 1 id field)
  * ``transformers_common_existencefilter``   → ``ExistenceFilterJob`` (already Spark)
  * ``import_content_url_dedup`` (PIG)        → ``DocumentContentUrlDedupJob`` (new Spark port)
  * ``content-url-dispatcher`` (MapReduce)    → ``DocumentContentUrlDispatcherJob`` (new Spark port)
  * ``report`` (ProcessWrapper/counters)      → produced directly by the Spark jobs as
                                                ``ReportEntry`` datastores (no extra task needed)

The two optional transformations are guarded by the Oozie ``<decision>`` nodes. In Airflow the
decisions are expressed with ``BranchPythonOperator``; the downstream input is selected with a
Jinja expression resolving to either the transformation output or its input depending on whether
the corresponding parameter is defined.
"""

import os
import sys
from datetime import timedelta

from airflow.decorators import dag
from airflow.models.param import Param
from airflow.providers.standard.operators.empty import EmptyOperator
from airflow.providers.standard.operators.python import BranchPythonOperator

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

_SCHEMA_DOCUMENT_CONTENT_URL = "eu.dnetlib.iis.importer.auxiliary.schemas.DocumentContentUrl"


def _defined(param_name: str) -> str:
    """Jinja boolean expression telling whether a DAG param is defined (not empty/$UNDEFINED$)."""
    return (f"params.get('{param_name}') and "
            f"params.get('{param_name}') != '{_UNDEFINED}'")


def _select(param_name: str, then_value: str, else_value: str) -> str:
    """Jinja template selecting `then_value` when the param is defined, otherwise `else_value`."""
    return ("{% if " + _defined(param_name) + " %}" + then_value
            + "{% else %}" + else_value + "{% endif %}")


def _decision_id_replacer(params):
    value = params.get("input_id_mapping")
    if value and value != _UNDEFINED:
        return ["transformers_idreplacer"]
    return ["decision_existence_filter"]


def _decision_existence_filter(params):
    value = params.get("input_id")
    if value and value != _UNDEFINED:
        return ["transformers_common_existencefilter"]
    return ["content_url_dedup"]


@dag(
    dag_id="spark_content_url_chain",
    dag_display_name="Imports DocumentContentUrl records and dispatches them by mime type",
    default_args=default_args,
    params={
        # ---------------- artifacts / runtime ----------------
        "JAR_IMPORT": Param(
            default="https://maven.ceon.pl/artifactory/iis-snapshots/eu/dnetlib/iis/iis-wf-import/1.3.0-SNAPSHOT/iis-wf-import-1.3.0-uber.jar",
            type="string",
            description="iis-wf-import uber JAR (content url importer, dedup, dispatcher)",
        ),
        "JAR_TRANSFORMERS": Param(
            default="https://maven.ceon.pl/artifactory/iis-snapshots/eu/dnetlib/iis/iis-wf-transformers/1.3.0-SNAPSHOT/iis-wf-transformers-1.3.0-uber.jar",
            type="string",
            description="iis-wf-transformers uber JAR (id replacer, existence filter)",
        ),
        "SPARK_IMAGE": Param(
            "docker-registry.openaire.eu/kubernetes_devel/spark:4.1.2", type="string"),
        "HADOOP_USER_NAME": Param("marek.horst", type="string"),

        # ---------------- input (hive aggregation subsystem) ----------------
        "input_table_name": Param(default="", type="string",
                                  description="Hive input table with content url records"),
        "hive_metastore_uris": Param(default="", type="string",
                                     description="Hive metastore URIs (empty → local metastore)"),
        "resultset_client_read_timeout": Param(default="60000", type="string"),
        "resultset_client_connection_timeout": Param(default="60000", type="string"),

        # ---------------- optional transformations ----------------
        "input_id_mapping": Param(
            default=_UNDEFINED, type="string",
            description="Id mappings used by the id-replacer (checksum → OA+ id deduplication). "
                        "$UNDEFINED$ → id-replacer is skipped."),
        "input_id": Param(
            default=_UNDEFINED, type="string",
            description="Identifiers existing in OA+ used by the existence filter. "
                        "$UNDEFINED$ → existence filter is skipped."),

        # ---------------- mime types ----------------
        "mimetypes_pdf": Param(default="pdf,application/pdf", type="string"),
        "mimetypes_html": Param(default="text/html", type="string"),
        "mimetypes_xml_pmc": Param(default="xml,application/xml", type="string"),
        "mimetypes_wos": Param(default="file::WoS", type="string"),

        # ---------------- output ----------------
        "workingDir": Param(
            default="hdfs://nameservice1/tmp/marek.horst/metadataextraction/content_url/working_dir",
            type="string",
            description="Working directory for the intermediate datastores",
        ),
        "output_root": Param(
            default="hdfs://nameservice1/tmp/marek.horst/metadataextraction/content_url/imported",
            type="string",
            description="Output root directory holding the per-mime-type subdirectories",
        ),
        "output_name_pdf": Param(default="pdf", type="string"),
        "output_name_html": Param(default="html", type="string"),
        "output_name_xml_pmc": Param(default="xmlpmc", type="string"),
        "output_name_wos": Param(default="wos", type="string"),

        # ---------------- reports ----------------
        "output_report_root_path": Param(
            default="", type="string",
            description="Base directory for partial reports. Empty → {workingDir}/report",
        ),
        "report_entry_pdf": Param(default="import.content.urls.bytype.pdf", type="string"),
        "report_entry_html": Param(default="import.content.urls.bytype.html", type="string"),
        "report_entry_xml_pmc": Param(default="import.content.urls.bytype.jats", type="string"),
        "report_entry_wos": Param(default="import.content.urls.bytype.wos", type="string"),

        # ---------------- spark tuning ----------------
        "sparkDriverMemory": Param(default="4g", type="string"),
        "sparkExecutorMemory": Param(default="4g", type="string"),
        "sparkExecutorOverhead": Param(default="1024", type="string"),
    },
    tags=["openaire", "iis", "import", "content_url"],
    schedule=None,
)
def content_url_chain():
    """content_url_core_parquet → (idreplacer) → (existence filter) → dedup → dispatcher."""

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

    # ------------------------------------------------------------------ #
    #  Paths (mirroring the Oozie working directory layout)              #
    # ------------------------------------------------------------------ #
    imported_urls      = "{{ params.get('workingDir') }}/imported-urls"
    idreplacer_out     = "{{ params.get('workingDir') }}/transformers_idreplacer/output"
    existencefilter_out = "{{ params.get('workingDir') }}/transformers_common_existencefilter/output"
    deduped_out        = "{{ params.get('workingDir') }}/document_content_url_deduped/output"

    report_root = "{{ params.get('output_report_root_path') or (params.get('workingDir') + '/report') }}"

    # input of the existence filter: id-replacer output when it ran, raw imported urls otherwise
    input_of_existence_filter = _select("input_id_mapping", idreplacer_out, imported_urls)
    # input of the dedup: existence filter output / id-replacer output / raw imported urls
    input_of_dedup = _select("input_id", existencefilter_out, input_of_existence_filter)

    # ------------------------------------------------------------------ #
    # 1. import_content_url_core_parquet                                 #
    # ------------------------------------------------------------------ #
    import_content_url_core = generate_spark_operator(
        task_id="import_content_url_core_parquet",
        task_display_name="Import DocumentContentUrl records from Hive (HiveBasedDocumentContentUrlImporterJob)",
        jar="{{ params.get('JAR_IMPORT') }}",
        image="{{ params.get('SPARK_IMAGE') }}",
        main_class="eu.dnetlib.iis.wf.importer.content.HiveBasedDocumentContentUrlImporterJob",
        arguments=[
            "-inputTableName",   "{{ params.get('input_table_name') }}",
            "-hiveMetastoreUris", "{{ params.get('hive_metastore_uris') }}",
            "-outputPath",       imported_urls,
            "-outputReportPath", report_root + "/import_content_url_from_aggregator",
        ],
        spark_extra_conf=spark_conf,
    )

    # ------------------------------------------------------------------ #
    # 2. transformers_idreplacer (optional)                              #
    # ------------------------------------------------------------------ #
    decision_id_replacer = BranchPythonOperator(
        task_id="decision_id_replacer",
        python_callable=_decision_id_replacer,
    )

    transformers_idreplacer = generate_spark_operator(
        task_id="transformers_idreplacer",
        task_display_name="Replace id with mapped identifiers (IdReplacerJob)",
        jar="{{ params.get('JAR_TRANSFORMERS') }}",
        image="{{ params.get('SPARK_IMAGE') }}",
        main_class="eu.dnetlib.iis.wf.transformers.idreplacer.IdReplacerJob",
        arguments=[
            "-input",             imported_urls,
            "-inputIdMapping",    "{{ params.get('input_id_mapping') }}",
            "-output",            idreplacer_out,
            "-schemaClass",       _SCHEMA_DOCUMENT_CONTENT_URL,
            "-idFieldToReplace1", "id",
        ],
        spark_extra_conf=spark_conf,
    )

    # ------------------------------------------------------------------ #
    # 3. transformers_common_existencefilter (optional)                  #
    # ------------------------------------------------------------------ #
    decision_existence_filter = BranchPythonOperator(
        task_id="decision_existence_filter",
        python_callable=_decision_existence_filter,
        trigger_rule="none_failed",
    )

    transformers_common_existencefilter = generate_spark_operator(
        task_id="transformers_common_existencefilter",
        task_display_name="Filter out contents without metadata representatives (ExistenceFilterJob)",
        jar="{{ params.get('JAR_TRANSFORMERS') }}",
        image="{{ params.get('SPARK_IMAGE') }}",
        main_class="eu.dnetlib.iis.wf.transformers.common.existencefilter.ExistenceFilterJob",
        arguments=[
            "-inputData",       input_of_existence_filter,
            "-inputExistentId", "{{ params.get('input_id') }}",
            "-outputFiltered",  existencefilter_out,
        ],
        spark_extra_conf=spark_conf,
    )

    # ------------------------------------------------------------------ #
    # 4. content_url_dedup (PIG → Spark)                                 #
    # ------------------------------------------------------------------ #
    content_url_dedup = generate_spark_operator(
        task_id="content_url_dedup",
        task_display_name="Deduplicate content urls by id and content checksum (DocumentContentUrlDedupJob)",
        jar="{{ params.get('JAR_IMPORT') }}",
        image="{{ params.get('SPARK_IMAGE') }}",
        main_class="eu.dnetlib.iis.wf.importer.content.DocumentContentUrlDedupJob",
        arguments=[
            "-input",  input_of_dedup,
            "-output", deduped_out,
        ],
        spark_extra_conf=spark_conf,
    )
    # the optional existence filter may be skipped → tolerate it here
    content_url_dedup.trigger_rule = "none_failed"

    # ------------------------------------------------------------------ #
    # 5. content-url-dispatcher (MapReduce → Spark)                      #
    # ------------------------------------------------------------------ #
    content_url_dispatcher = generate_spark_operator(
        task_id="content_url_dispatcher",
        task_display_name="Dispatch content urls by mime type (DocumentContentUrlDispatcherJob)",
        jar="{{ params.get('JAR_IMPORT') }}",
        image="{{ params.get('SPARK_IMAGE') }}",
        main_class="eu.dnetlib.iis.wf.importer.content.DocumentContentUrlDispatcherJob",
        arguments=[
            "-input",              deduped_out,
            "-outputRoot",         "{{ params.get('output_root') }}",
            "-outputReportPath",   report_root + "/import_content_url",
            "-outputNamePdf",      "{{ params.get('output_name_pdf') }}",
            "-outputNameHtml",     "{{ params.get('output_name_html') }}",
            "-outputNameXmlPmc",   "{{ params.get('output_name_xml_pmc') }}",
            "-outputNameWos",      "{{ params.get('output_name_wos') }}",
            "-mimetypesPdf",       "{{ params.get('mimetypes_pdf') }}",
            "-mimetypesHtml",      "{{ params.get('mimetypes_html') }}",
            "-mimetypesXmlPmc",    "{{ params.get('mimetypes_xml_pmc') }}",
            "-mimetypesWos",       "{{ params.get('mimetypes_wos') }}",
            "-reportEntryPdf",     "{{ params.get('report_entry_pdf') }}",
            "-reportEntryHtml",    "{{ params.get('report_entry_html') }}",
            "-reportEntryXmlPmc",  "{{ params.get('report_entry_xml_pmc') }}",
            "-reportEntryWos",     "{{ params.get('report_entry_wos') }}",
        ],
        spark_extra_conf=spark_conf,
    )

    end = EmptyOperator(task_id="end", task_display_name="Content url chain finished")

    # ------------------------------------------------------------------ #
    # Wiring                                                             #
    # ------------------------------------------------------------------ #
    import_content_url_core >> decision_id_replacer
    decision_id_replacer >> [transformers_idreplacer, decision_existence_filter]
    transformers_idreplacer >> decision_existence_filter
    decision_existence_filter >> [transformers_common_existencefilter, content_url_dedup]
    transformers_common_existencefilter >> content_url_dedup
    content_url_dedup >> content_url_dispatcher >> end


content_url_chain()
