import os
import sys
from datetime import timedelta

from airflow.decorators import dag
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
from spark_configurator import generate_spark_operator

EXECUTION_TIMEOUT = int(os.getenv("EXECUTION_TIMEOUT", 6))

default_args = {
    "execution_timeout": timedelta(days=EXECUTION_TIMEOUT),
    "retries": int(os.getenv("DEFAULT_TASK_RETRIES", 1)),
    "retry_delay": timedelta(seconds=int(os.getenv("DEFAULT_RETRY_DELAY_SECONDS", 60)))
}

_EXPORTER_CLASS = "eu.dnetlib.iis.wf.export.actionmanager.sequencefile.SequenceFileExporterJob"

# Fully-qualified Avro schema class names for each inference type.
_SCHEMA_DOCUMENT_TO_PROJECT     = "eu.dnetlib.iis.referenceextraction.project.schemas.DocumentToProject"
_SCHEMA_DOCUMENT_TO_DATASET     = "eu.dnetlib.iis.referenceextraction.dataset.schemas.DocumentToDataSet"
_SCHEMA_DOCUMENT_TO_PATENT      = "eu.dnetlib.iis.referenceextraction.patent.schemas.DocumentToPatent"
_SCHEMA_DOCUMENT_TO_SERVICE     = "eu.dnetlib.iis.referenceextraction.service.schemas.DocumentToService"
_SCHEMA_DOCUMENT_TO_CONCEPT_IDS = "eu.dnetlib.iis.export.schemas.DocumentToConceptIds"
_SCHEMA_DOCUMENT_TO_DOC_CLASSES = "eu.dnetlib.iis.documentsclassification.schemas.DocumentToDocumentClasses"
_SCHEMA_CITATIONS               = "eu.dnetlib.iis.export.schemas.Citations"
_SCHEMA_DOCUMENT_SIMILARITY     = "eu.dnetlib.iis.documentssimilarity.schemas.DocumentSimilarity"
_SCHEMA_MATCHED_ORG             = "eu.dnetlib.iis.wf.affmatching.model.MatchedOrganizationWithProvenance"

# ActionBuilderFactory class names.
_FACTORY_DOCUMENT_TO_PROJECT     = "eu.dnetlib.iis.wf.export.actionmanager.module.DocumentToProjectActionBuilderModuleFactory"
_FACTORY_DOCUMENT_TO_DATASET     = "eu.dnetlib.iis.wf.export.actionmanager.module.DocumentToDataSetActionBuilderModuleFactory"
_FACTORY_DOCUMENT_TO_PATENT      = "eu.dnetlib.iis.wf.export.actionmanager.module.DocumentToPatentActionBuilderModuleFactory"
_FACTORY_DOCUMENT_TO_SERVICE     = "eu.dnetlib.iis.wf.export.actionmanager.module.DocumentToServiceActionBuilderModuleFactory"
_FACTORY_DOCUMENT_TO_CONCEPT_IDS = "eu.dnetlib.iis.wf.export.actionmanager.module.DocumentToConceptIdsActionBuilderModuleFactory"
_FACTORY_DOCUMENT_TO_COMMUNITY   = "eu.dnetlib.iis.wf.export.actionmanager.module.DocumentToCommunityActionBuilderModuleFactory"
_FACTORY_DOCUMENT_TO_PDB         = "eu.dnetlib.iis.wf.export.actionmanager.module.DocumentToPdbActionBuilderModuleFactory"
_FACTORY_DOCUMENT_TO_COVID19     = "eu.dnetlib.iis.wf.export.actionmanager.module.DocumentToCovid19ActionBuilderModuleFactory"
_FACTORY_DOCUMENT_TO_DOC_CLASSES = "eu.dnetlib.iis.wf.export.actionmanager.module.DocumentToDocumentClassesActionBuilderModuleFactory"
_FACTORY_CITATIONS               = "eu.dnetlib.iis.wf.export.actionmanager.module.CitationsActionBuilderModuleFactory"
_FACTORY_DOCUMENT_SIMILARITY     = "eu.dnetlib.iis.wf.export.actionmanager.module.DocumentSimilarityActionBuilderModuleFactory"
_FACTORY_MATCHED_ORG             = "eu.dnetlib.iis.wf.export.actionmanager.module.MatchedOrganizationActionBuilderModuleFactory"


@dag(
    dag_id="spark_sequencefile_exporter",
    dag_display_name="Exports inferences as AtomicAction SequenceFiles for the ActionManager service",
    default_args=default_args,
    params={
        "JAR": Param(
            default="https://maven.ceon.pl/artifactory/iis-snapshots/eu/dnetlib/iis/iis-wf-export-actionmanager/1.3.0-SNAPSHOT/iis-wf-export-actionmanager-1.3.0-20260827.112120-6-uber.jar",
            type="string",
            description="Shaded (uber) JAR of the iis-wf-export-actionmanager module",
        ),
        "SPARK_IMAGE": Param(
            "docker-registry.openaire.eu/kubernetes_devel/spark:4.1.2",
            type="string",
            description="Spark Docker image used for all export tasks",
        ),
        "HADOOP_USER_NAME": Param(
            "marek.horst",
            type="string",
            description="Hadoop/HDFS user name propagated to driver and executor pods",
        ),

        # ------------------------------------------------------------------ #
        # Input paths — set to $UNDEFINED$ to skip the corresponding export.  #
        # ------------------------------------------------------------------ #
        "input_document_to_project": Param(
            default="$UNDEFINED$",
            type="string",
            description="Avro datastore with DocumentToProject inferences "
                        "(eu.dnetlib.iis.referenceextraction.project.schemas.DocumentToProject). "
                        "Leave as $UNDEFINED$ to skip.",
        ),
        "input_document_to_dataset": Param(
            default="$UNDEFINED$",
            type="string",
            description="Avro datastore with DocumentToDataSet inferences "
                        "(eu.dnetlib.iis.referenceextraction.dataset.schemas.DocumentToDataSet). "
                        "Leave as $UNDEFINED$ to skip.",
        ),
        "input_document_to_patent": Param(
            default="$UNDEFINED$",
            type="string",
            description="Avro datastore with DocumentToPatent inferences "
                        "(eu.dnetlib.iis.referenceextraction.patent.schemas.DocumentToPatent). "
                        "Leave as $UNDEFINED$ to skip.",
        ),
        "input_document_to_service": Param(
            default="$UNDEFINED$",
            type="string",
            description="Avro datastore with DocumentToService inferences "
                        "(eu.dnetlib.iis.referenceextraction.service.schemas.DocumentToService). "
                        "Leave as $UNDEFINED$ to skip.",
        ),
        "input_document_to_research_initiatives": Param(
            default="$UNDEFINED$",
            type="string",
            description="Avro datastore with DocumentToConceptIds (research-initiative) inferences. "
                        "Leave as $UNDEFINED$ to skip.",
        ),
        "input_document_to_community": Param(
            default="$UNDEFINED$",
            type="string",
            description="Avro datastore with DocumentToConceptIds (community) inferences. "
                        "Leave as $UNDEFINED$ to skip.",
        ),
        "input_document_to_pdb": Param(
            default="$UNDEFINED$",
            type="string",
            description="Avro datastore with DocumentToConceptIds (Protein Data Bank) inferences. "
                        "Leave as $UNDEFINED$ to skip.",
        ),
        "input_document_to_covid19": Param(
            default="$UNDEFINED$",
            type="string",
            description="Avro datastore with DocumentToConceptIds (COVID-19) inferences. "
                        "Leave as $UNDEFINED$ to skip.",
        ),
        "input_document_to_document_classes": Param(
            default="$UNDEFINED$",
            type="string",
            description="Avro datastore with DocumentToDocumentClasses inferences "
                        "(eu.dnetlib.iis.documentsclassification.schemas.DocumentToDocumentClasses). "
                        "Leave as $UNDEFINED$ to skip.",
        ),
        "input_citations": Param(
            default="$UNDEFINED$",
            type="string",
            description="Avro datastore with Citations inferences "
                        "(eu.dnetlib.iis.export.schemas.Citations). "
                        "Leave as $UNDEFINED$ to skip.",
        ),
        "input_document_similarity": Param(
            default="$UNDEFINED$",
            type="string",
            description="Avro datastore with DocumentSimilarity inferences "
                        "(eu.dnetlib.iis.documentssimilarity.schemas.DocumentSimilarity). "
                        "Leave as $UNDEFINED$ to skip.",
        ),
        "input_matched_doc_organizations": Param(
            default="$UNDEFINED$",
            type="string",
            description="Avro datastore with MatchedOrganizationWithProvenance inferences "
                        "(eu.dnetlib.iis.wf.affmatching.model.MatchedOrganizationWithProvenance). "
                        "Leave as $UNDEFINED$ to skip.",
        ),

        # ------------------------------------------------------------------ #
        # Output                                                               #
        # ------------------------------------------------------------------ #
        "output": Param(
            default="hdfs://iis-cdh5-test-m2.ocean.icm.edu.pl:8020/tmp/marek.horst/export_actionmanager/output",
            type="string",
            description="Base HDFS output directory. Each export action writes to a dedicated "
                        "subdirectory under this path: {output}/{type}/{action_set_id}.",
        ),

        # ------------------------------------------------------------------ #
        # Action-set identifiers                                               #
        # ------------------------------------------------------------------ #
        "action_set_id_document_referencedProjects": Param(
            default="document_referencedProjects",
            type="string",
            description="Action-set identifier for document → referenced-projects export",
        ),
        "action_set_id_document_referencedDatasets": Param(
            default="document_referencedDatasets",
            type="string",
            description="Action-set identifier for document → referenced-datasets export",
        ),
        "action_set_id_document_patent": Param(
            default="document_patent",
            type="string",
            description="Action-set identifier for document → patent export",
        ),
        "action_set_id_document_eoscServices": Param(
            default="document_eoscServices",
            type="string",
            description="Action-set identifier for document → EOSC services export",
        ),
        "action_set_id_document_research_initiative": Param(
            default="document_research_initiative",
            type="string",
            description="Action-set identifier for document → research-initiative export",
        ),
        "action_set_id_document_community": Param(
            default="document_community",
            type="string",
            description="Action-set identifier for document → community export",
        ),
        "action_set_id_document_pdb": Param(
            default="document_pdb",
            type="string",
            description="Action-set identifier for document → Protein Data Bank export",
        ),
        "action_set_id_document_covid19": Param(
            default="document_covid19",
            type="string",
            description="Action-set identifier for document → COVID-19 export",
        ),
        "action_set_id_document_classes": Param(
            default="document_classes",
            type="string",
            description="Action-set identifier for document → document-classes export",
        ),
        "action_set_id_document_referencedDocuments": Param(
            default="document_referencedDocuments",
            type="string",
            description="Action-set identifier for citation (document → referenced-documents) export",
        ),
        "action_set_id_document_similarities_standard": Param(
            default="document_similarities_standard",
            type="string",
            description="Action-set identifier for document-similarity export",
        ),
        "action_set_id_matched_doc_organizations": Param(
            default="matched_doc_organizations",
            type="string",
            description="Action-set identifier for document → matched-organization export",
        ),

        # ------------------------------------------------------------------ #
        # Trust-level thresholds                                               #
        # ------------------------------------------------------------------ #
        "trust_level_threshold": Param(
            default="$UNDEFINED$",
            type="string",
            description="Default trust-level threshold applied to all export actions "
                        "(export.trust.level.threshold). Set to $UNDEFINED$ to disable.",
        ),
        "trust_level_threshold_document_classes": Param(
            default="$UNDEFINED$",
            type="string",
            description="Per-algorithm trust-level threshold for document_classes "
                        "(export.trust.level.threshold.document_classes).",
        ),
        "trust_level_threshold_document_referencedProjects": Param(
            default="$UNDEFINED$",
            type="string",
            description="Per-algorithm trust-level threshold for document_referencedProjects "
                        "(export.trust.level.threshold.document_referencedProjects).",
        ),
        "trust_level_threshold_document_referencedDatasets": Param(
            default="$UNDEFINED$",
            type="string",
            description="Per-algorithm trust-level threshold for document_referencedDatasets "
                        "(export.trust.level.threshold.document_referencedDatasets).",
        ),
        "trust_level_threshold_document_patent": Param(
            default="$UNDEFINED$",
            type="string",
            description="Per-algorithm trust-level threshold for document_patent "
                        "(export.trust.level.threshold.document_patent).",
        ),
        "trust_level_threshold_document_referencedDocuments": Param(
            default="$UNDEFINED$",
            type="string",
            description="Per-algorithm trust-level threshold for document_referencedDocuments "
                        "(export.trust.level.threshold.document_referencedDocuments).",
        ),
        "trust_level_threshold_document_pdb": Param(
            default="$UNDEFINED$",
            type="string",
            description="Per-algorithm trust-level threshold for document_pdb "
                        "(export.trust.level.threshold.document_pdb).",
        ),
        "trust_level_threshold_matched_doc_organizations": Param(
            default="$UNDEFINED$",
            type="string",
            description="Per-algorithm trust-level threshold for document_affiliations "
                        "(export.trust.level.threshold.document_affiliations).",
        ),

        # ------------------------------------------------------------------ #
        # Additional algorithm-specific parameters                             #
        # ------------------------------------------------------------------ #
        "collectedfrom_key": Param(
            default="10|infrastruct_::f66f1bd369679b5b077dcdf006089556",
            type="string",
            description="Datasource identifier stored in Relation#collectedfrom[].value "
                        "(export.relation.collectedfrom.key).",
        ),
        "documentssimilarity_threshold": Param(
            default="0.7",
            type="string",
            description="Similarity score threshold below which a similarity record is not exported "
                        "(export.documentssimilarity.threshold).",
        ),
        "referenceextraction_pdb_url_root": Param(
            default="http://www.rcsb.org/pdb/explore/explore.do?structureId=",
            type="string",
            description="Protein Data Bank URL root prepended to PDB identifiers when forming the "
                        "final reference URL (export.referenceextraction.pdb.url.root).",
        ),

        # ------------------------------------------------------------------ #
        # Output-file control                                                  #
        # ------------------------------------------------------------------ #
        "numberOfOutputFiles": Param(
            default="1",
            type="string",
            description="Number of output SequenceFile parts produced by export actions that handle "
                        "a large number of small input files (document_referencedProjects, "
                        "document_referencedDatasets, document_patent, document_eoscServices, "
                        "document_classes, matched_doc_organizations). "
                        "Set to 0 to preserve the natural partition count of the input.",
        ),

        # ------------------------------------------------------------------ #
        # Spark resource tuning                                                #
        # ------------------------------------------------------------------ #
        "sparkDriverMemory": Param(
            default="10g",
            type="string",
            description="Memory allocated to the Spark driver",
        ),
        "sparkExecutorMemory": Param(
            default="10g",
            type="string",
            description="Memory allocated to each Spark executor",
        ),
    },
    tags=["openaire", "iis", "export", "actionmanager"],
    schedule=None,
)
def sequencefile_exporter():

    spark_conf = {
        "spark.driver.memory":   "{{ params.get('sparkDriverMemory') }}",
        "spark.executor.memory": "{{ params.get('sparkExecutorMemory') }}",
        "spark.driverEnv.HADOOP_USER_NAME":               "{{ params.get('HADOOP_USER_NAME') }}",
        "spark.executorEnv.HADOOP_USER_NAME":             "{{ params.get('HADOOP_USER_NAME') }}",
        "spark.driverEnv.SPARK_USER":                     "{{ params.get('HADOOP_USER_NAME') }}",
        "spark.executorEnv.SPARK_USER":                   "{{ params.get('HADOOP_USER_NAME') }}",
        "spark.kubernetes.driverEnv.HADOOP_USER_NAME":    "{{ params.get('HADOOP_USER_NAME') }}",
        "spark.kubernetes.executorEnv.HADOOP_USER_NAME":  "{{ params.get('HADOOP_USER_NAME') }}",
    }

    # ------------------------------------------------------------------ #
    # Export actions that benefit from repartitioning (large input sets)  #
    # mirror mapreduce.job.reduces = numberOfOutputFiles in the Oozie workflow.  #
    # ------------------------------------------------------------------ #

    generate_spark_operator(
        task_id="exporter_document_to_project",
        task_display_name="Export document → referenced projects",
        jar="{{ params.get('JAR') }}",
        image="{{ params.get('SPARK_IMAGE') }}",
        main_class=_EXPORTER_CLASS,
        arguments=[
            "-inputPath",
            "{{ params.get('input_document_to_project') }}",
            "-outputPath",
            "{{ params.get('output') }}/document_referencedProjects/{{ params.get('action_set_id_document_referencedProjects') }}",
            "-actionBuilderFactoryClassName", _FACTORY_DOCUMENT_TO_PROJECT,
            "-inputAvroSchemaClass",          _SCHEMA_DOCUMENT_TO_PROJECT,
            "-numberOfOutputFiles",           "{{ params.get('numberOfOutputFiles') }}",
            "-Dexport.trust.level.threshold={{ params.get('trust_level_threshold') }}",
            "-Dexport.relation.collectedfrom.key={{ params.get('collectedfrom_key') }}",
            "-Dexport.trust.level.threshold.document_referencedProjects={{ params.get('trust_level_threshold_document_referencedProjects') }}",
        ],
        spark_extra_conf=spark_conf,
    )

    generate_spark_operator(
        task_id="exporter_document_to_dataset",
        task_display_name="Export document → referenced datasets",
        jar="{{ params.get('JAR') }}",
        image="{{ params.get('SPARK_IMAGE') }}",
        main_class=_EXPORTER_CLASS,
        arguments=[
            "-inputPath",
            "{{ params.get('input_document_to_dataset') }}",
            "-outputPath",
            "{{ params.get('output') }}/document_referencedDatasets/{{ params.get('action_set_id_document_referencedDatasets') }}",
            "-actionBuilderFactoryClassName", _FACTORY_DOCUMENT_TO_DATASET,
            "-inputAvroSchemaClass",          _SCHEMA_DOCUMENT_TO_DATASET,
            "-numberOfOutputFiles",           "{{ params.get('numberOfOutputFiles') }}",
            "-Dexport.trust.level.threshold={{ params.get('trust_level_threshold') }}",
            "-Dexport.relation.collectedfrom.key={{ params.get('collectedfrom_key') }}",
            "-Dexport.trust.level.threshold.document_referencedDatasets={{ params.get('trust_level_threshold_document_referencedDatasets') }}",
        ],
        spark_extra_conf=spark_conf,
    )

    generate_spark_operator(
        task_id="exporter_document_to_patent",
        task_display_name="Export document → patents",
        jar="{{ params.get('JAR') }}",
        image="{{ params.get('SPARK_IMAGE') }}",
        main_class=_EXPORTER_CLASS,
        arguments=[
            "-inputPath",
            "{{ params.get('input_document_to_patent') }}",
            "-outputPath",
            "{{ params.get('output') }}/document_patent/{{ params.get('action_set_id_document_patent') }}",
            "-actionBuilderFactoryClassName", _FACTORY_DOCUMENT_TO_PATENT,
            "-inputAvroSchemaClass",          _SCHEMA_DOCUMENT_TO_PATENT,
            "-numberOfOutputFiles",           "{{ params.get('numberOfOutputFiles') }}",
            "-Dexport.trust.level.threshold={{ params.get('trust_level_threshold') }}",
            "-Dexport.relation.collectedfrom.key={{ params.get('collectedfrom_key') }}",
            "-Dexport.trust.level.threshold.document_patent={{ params.get('trust_level_threshold_document_patent') }}",
        ],
        spark_extra_conf=spark_conf,
    )

    generate_spark_operator(
        task_id="exporter_document_to_service",
        task_display_name="Export document → EOSC services",
        jar="{{ params.get('JAR') }}",
        image="{{ params.get('SPARK_IMAGE') }}",
        main_class=_EXPORTER_CLASS,
        arguments=[
            "-inputPath",
            "{{ params.get('input_document_to_service') }}",
            "-outputPath",
            "{{ params.get('output') }}/document_eoscServices/{{ params.get('action_set_id_document_eoscServices') }}",
            "-actionBuilderFactoryClassName", _FACTORY_DOCUMENT_TO_SERVICE,
            "-inputAvroSchemaClass",          _SCHEMA_DOCUMENT_TO_SERVICE,
            "-numberOfOutputFiles",           "{{ params.get('numberOfOutputFiles') }}",
            "-Dexport.trust.level.threshold={{ params.get('trust_level_threshold') }}",
            "-Dexport.relation.collectedfrom.key={{ params.get('collectedfrom_key') }}",
        ],
        spark_extra_conf=spark_conf,
    )

    generate_spark_operator(
        task_id="exporter_document_to_document_classes",
        task_display_name="Export document → document classes",
        jar="{{ params.get('JAR') }}",
        image="{{ params.get('SPARK_IMAGE') }}",
        main_class=_EXPORTER_CLASS,
        arguments=[
            "-inputPath",
            "{{ params.get('input_document_to_document_classes') }}",
            "-outputPath",
            "{{ params.get('output') }}/document_classes/{{ params.get('action_set_id_document_classes') }}",
            "-actionBuilderFactoryClassName", _FACTORY_DOCUMENT_TO_DOC_CLASSES,
            "-inputAvroSchemaClass",          _SCHEMA_DOCUMENT_TO_DOC_CLASSES,
            "-numberOfOutputFiles",           "{{ params.get('numberOfOutputFiles') }}",
            "-Dexport.trust.level.threshold={{ params.get('trust_level_threshold') }}",
            "-Dexport.relation.collectedfrom.key={{ params.get('collectedfrom_key') }}",
            "-Dexport.trust.level.threshold.document_classes={{ params.get('trust_level_threshold_document_classes') }}",
        ],
        spark_extra_conf=spark_conf,
    )

    generate_spark_operator(
        task_id="exporter_matched_doc_organizations",
        task_display_name="Export document → matched organizations",
        jar="{{ params.get('JAR') }}",
        image="{{ params.get('SPARK_IMAGE') }}",
        main_class=_EXPORTER_CLASS,
        arguments=[
            "-inputPath",
            "{{ params.get('input_matched_doc_organizations') }}",
            "-outputPath",
            "{{ params.get('output') }}/matched_doc_organizations/{{ params.get('action_set_id_matched_doc_organizations') }}",
            "-actionBuilderFactoryClassName", _FACTORY_MATCHED_ORG,
            "-inputAvroSchemaClass",          _SCHEMA_MATCHED_ORG,
            "-numberOfOutputFiles",           "{{ params.get('numberOfOutputFiles') }}",
            "-Dexport.trust.level.threshold={{ params.get('trust_level_threshold') }}",
            "-Dexport.relation.collectedfrom.key={{ params.get('collectedfrom_key') }}",
            # The Oozie workflow maps trust_level_threshold_matched_doc_organizations
            # to export.trust.level.threshold.document_affiliations (the AlgorithmName).
            "-Dexport.trust.level.threshold.document_affiliations={{ params.get('trust_level_threshold_matched_doc_organizations') }}",
        ],
        spark_extra_conf=spark_conf,
    )

    # ------------------------------------------------------------------ #
    # Export actions without repartitioning (map-only equivalent,         #
    # mirror mapreduce.job.reduces=0 in the Oozie workflow).              #
    # ------------------------------------------------------------------ #

    generate_spark_operator(
        task_id="exporter_document_to_research_initiatives",
        task_display_name="Export document → research initiatives",
        jar="{{ params.get('JAR') }}",
        image="{{ params.get('SPARK_IMAGE') }}",
        main_class=_EXPORTER_CLASS,
        arguments=[
            "-inputPath",
            "{{ params.get('input_document_to_research_initiatives') }}",
            "-outputPath",
            "{{ params.get('output') }}/document_research_initiative/{{ params.get('action_set_id_document_research_initiative') }}",
            "-actionBuilderFactoryClassName", _FACTORY_DOCUMENT_TO_CONCEPT_IDS,
            "-inputAvroSchemaClass",          _SCHEMA_DOCUMENT_TO_CONCEPT_IDS,
            "-Dexport.trust.level.threshold={{ params.get('trust_level_threshold') }}",
            "-Dexport.relation.collectedfrom.key={{ params.get('collectedfrom_key') }}",
        ],
        spark_extra_conf=spark_conf,
    )

    generate_spark_operator(
        task_id="exporter_document_to_community",
        task_display_name="Export document → communities",
        jar="{{ params.get('JAR') }}",
        image="{{ params.get('SPARK_IMAGE') }}",
        main_class=_EXPORTER_CLASS,
        arguments=[
            "-inputPath",
            "{{ params.get('input_document_to_community') }}",
            "-outputPath",
            "{{ params.get('output') }}/document_community/{{ params.get('action_set_id_document_community') }}",
            "-actionBuilderFactoryClassName", _FACTORY_DOCUMENT_TO_COMMUNITY,
            "-inputAvroSchemaClass",          _SCHEMA_DOCUMENT_TO_CONCEPT_IDS,
            "-Dexport.trust.level.threshold={{ params.get('trust_level_threshold') }}",
            "-Dexport.relation.collectedfrom.key={{ params.get('collectedfrom_key') }}",
        ],
        spark_extra_conf=spark_conf,
    )

    generate_spark_operator(
        task_id="exporter_document_to_pdb",
        task_display_name="Export document → Protein Data Bank",
        jar="{{ params.get('JAR') }}",
        image="{{ params.get('SPARK_IMAGE') }}",
        main_class=_EXPORTER_CLASS,
        arguments=[
            "-inputPath",
            "{{ params.get('input_document_to_pdb') }}",
            "-outputPath",
            "{{ params.get('output') }}/document_pdb/{{ params.get('action_set_id_document_pdb') }}",
            "-actionBuilderFactoryClassName", _FACTORY_DOCUMENT_TO_PDB,
            "-inputAvroSchemaClass",          _SCHEMA_DOCUMENT_TO_CONCEPT_IDS,
            "-Dexport.trust.level.threshold={{ params.get('trust_level_threshold') }}",
            "-Dexport.relation.collectedfrom.key={{ params.get('collectedfrom_key') }}",
            "-Dexport.trust.level.threshold.document_pdb={{ params.get('trust_level_threshold_document_pdb') }}",
            "-Dexport.referenceextraction.pdb.url.root={{ params.get('referenceextraction_pdb_url_root') }}",
        ],
        spark_extra_conf=spark_conf,
    )

    generate_spark_operator(
        task_id="exporter_document_to_covid19",
        task_display_name="Export document → COVID-19",
        jar="{{ params.get('JAR') }}",
        image="{{ params.get('SPARK_IMAGE') }}",
        main_class=_EXPORTER_CLASS,
        arguments=[
            "-inputPath",
            "{{ params.get('input_document_to_covid19') }}",
            "-outputPath",
            "{{ params.get('output') }}/document_covid19/{{ params.get('action_set_id_document_covid19') }}",
            "-actionBuilderFactoryClassName", _FACTORY_DOCUMENT_TO_COVID19,
            "-inputAvroSchemaClass",          _SCHEMA_DOCUMENT_TO_CONCEPT_IDS,
            "-Dexport.trust.level.threshold={{ params.get('trust_level_threshold') }}",
            "-Dexport.relation.collectedfrom.key={{ params.get('collectedfrom_key') }}",
        ],
        spark_extra_conf=spark_conf,
    )

    generate_spark_operator(
        task_id="exporter_citation",
        task_display_name="Export citations (document → referenced documents)",
        jar="{{ params.get('JAR') }}",
        image="{{ params.get('SPARK_IMAGE') }}",
        main_class=_EXPORTER_CLASS,
        arguments=[
            "-inputPath",
            "{{ params.get('input_citations') }}",
            "-outputPath",
            "{{ params.get('output') }}/document_referencedDocuments/{{ params.get('action_set_id_document_referencedDocuments') }}",
            "-actionBuilderFactoryClassName", _FACTORY_CITATIONS,
            "-inputAvroSchemaClass",          _SCHEMA_CITATIONS,
            "-Dexport.trust.level.threshold={{ params.get('trust_level_threshold') }}",
            "-Dexport.relation.collectedfrom.key={{ params.get('collectedfrom_key') }}",
            "-Dexport.trust.level.threshold.document_referencedDocuments={{ params.get('trust_level_threshold_document_referencedDocuments') }}",
        ],
        spark_extra_conf=spark_conf,
    )

    generate_spark_operator(
        task_id="exporter_document_similarity",
        task_display_name="Export document similarities",
        jar="{{ params.get('JAR') }}",
        image="{{ params.get('SPARK_IMAGE') }}",
        main_class=_EXPORTER_CLASS,
        arguments=[
            "-inputPath",
            "{{ params.get('input_document_similarity') }}",
            "-outputPath",
            "{{ params.get('output') }}/document_similarities_standard/{{ params.get('action_set_id_document_similarities_standard') }}",
            "-actionBuilderFactoryClassName", _FACTORY_DOCUMENT_SIMILARITY,
            "-inputAvroSchemaClass",          _SCHEMA_DOCUMENT_SIMILARITY,
            "-Dexport.trust.level.threshold={{ params.get('trust_level_threshold') }}",
            "-Dexport.relation.collectedfrom.key={{ params.get('collectedfrom_key') }}",
            "-Dexport.documentssimilarity.threshold={{ params.get('documentssimilarity_threshold') }}",
        ],
        spark_extra_conf=spark_conf,
    )


sequencefile_exporter()
