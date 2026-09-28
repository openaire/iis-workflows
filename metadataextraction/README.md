# Metadata extraction — Oozie → Airflow/Spark 4 migration

This directory holds the Airflow workflow definitions replacing the metadata extraction (and
cache builder) Oozie workflows so that they can run in the new Kubernetes environment on Spark 4.

## Deliverables

### Java Spark jobs (in the `iis` repository)

| New class | Replaces | Notes |
|---|---|---|
| `eu.dnetlib.iis.wf.metadataextraction.MetadataExtractorJob` | `MetadataExtractorMapper` (MapReduce) + the `metadataextraction/core` Oozie workflow | PDF metadata extraction via Grobid or embedded CERMINE; writes `meta`/`fault`/`transientfault` datastores and `import.metadataExtraction.processed.*` report entries. Exposes `extract(...)`/`emptyResult(...)` reused in-process. |
| `eu.dnetlib.iis.wf.metadataextraction.cache.MetadataExtractionCacheChainJob` | `cache/chain` + `cache/create` + `cache/update` Oozie workflows | Cache management (skip-extracted, cache id, lock, create/update, union of returned meta, fault handling) offloaded into Spark, using `CacheStorageUtils`/`CacheMetadataManagingProcess`/`LockManagerUtils` — the same pattern as `CachedWebCrawlerJob`. |
| `eu.dnetlib.iis.wf.importer.content.DocumentContentUrlBasedImporterJob` | `importer/content` (MapReduce `DocumentContentUrlBasedImporterMapper`) | Object-store content retrieval (`DocumentContentUrl` → `DocumentContent`). Reuses `ObjectStoreContentProviderUtils`/`ContentRetrievalContext`. |
| `eu.dnetlib.iis.wf.importer.content.DocumentContentUrlDedupJob` | `content_url/dedup` PIG `dedup.pig` | Deduplicates by `(id, contentChecksum)`. |
| `eu.dnetlib.iis.wf.importer.content.DocumentContentUrlDispatcherJob` | `content_url/chain` MapReduce `DocumentContentUrlDispatcher` | Mime-type based dispatch to `pdf`/`html`/`xmlpmc`/`wos` ports + per-type report entries. |

Already ported Spark jobs are **reused as-is**: `ChecksumPreprocessingJob`,
`SkipExtractedWithoutMetaJob` (logic absorbed by the cache chain job), `UnionJob`,
`ChecksumMetaPostprocessingJob`, `ChecksumFaultPostprocessingJob`, `IdReplacerJob`,
`ExistenceFilterJob`, `HiveBasedDocumentContentUrlImporterJob`.

### Airflow DAGs (in this repository)

| DAG | Replaces | Must stay Airflow |
|---|---|---|
| `import/spark_content_url_chain_dag.py` (`spark_content_url_chain`) | `importer/content_url/chain` | yes — reused by many IIS workflows |
| `metadataextraction/spark_metadataextraction_cache_identify_by_checksum_dag.py` | `metadataextraction/cache/identify_by_checksum` | yes — top level workflow |
| `metadataextraction/spark_metadataextraction_cache_builder_dag.py` | `metadataextraction/cache/builder` | yes — top level workflow |

## Approach: 1-to-1 mapping vs. simplification

Per the task description the choice should be driven by **reusability** and the **existence of
already-utilised Airflow workflows**. Both factors were applied:

* The three workflows explicitly listed as reusable remain **Airflow DAGs** and are called rather
  than duplicated: `identify_by_checksum`, `cache/builder` and `importer/content_url/chain`
  (the latter triggered via `TriggerDagRunOperator` so it can be reused by other workflows).
* Everything else that was **not** reusable and only existed for the sake of Oozie's cumbersome
  conditional transitions was **offloaded into Spark** — most notably the whole cache hierarchy
  (`cache/chain` + `cache/create` + `cache/update`) with its lock and cache-id management
  utility actions. This removes ZooKeeper/HDFS lock actions, the `check_input_isempty` +
  `decision` pairs and the `cache_create`/`cache_update` duplication from the workflow layer,
  exactly as suggested by the `CachedWebCrawlerJob` precedent.
* For Oozie sub-workflows that effectively covered **a single action** (rule of thumb from the
  task description), no Airflow wrapper was created — the corresponding Spark job is called
  directly from the parent DAG (e.g. `transformers_idreplacer`, `transformers_common_existencefilter`,
  `content_url/dedup`, `postprocessing-meta`/`-fault`).

## Notable simplifications / deviations

* **Emptiness checks** (`check_input_isempty` / `generate-empty-output`): handled inside
  `MetadataExtractionCacheChainJob`, which writes empty `meta`/`fault` datastores for empty input.
  The outcome is identical to the Oozie branching, without any Airflow branch.
* **Schema generation** (`AvroSchemaGenerator` Oozie actions): unnecessary in Spark — schemas are
  compile-time constants.
* **`ReportGenerator` / `PigCountersReportGenerator` / `OozieTimeReportGenerator`**: replaced by
  writing `ReportEntry` Avro datastores directly from the Spark jobs (established convention),
  which are then merged by `ReportMerger`.
* **`PigCountersReportGenerator` counters** for the PIG-ported transformers
  (`skip_extracted.fromCache.docMetadata`) are emitted directly by the cache chain job as
  `<prefix>.fromCache.docMetadata` report entries.
* **`OozieTimeReportGenerator`** (execution-time reports based on the Oozie service) has no
  equivalent — task durations are available natively from Airflow.
* **Report builder output path**: the merged `report.json` is written to
  `{workingDir}/report_builder/report.json` (instead of `{workingDir}/report/working_dir/report.json`)
  so that it is not picked up as a partial-report subdirectory on reruns. The report *builder*
  workflow is otherwise ported 1-to-1 via `ProcessWrapper` + `ReportMerger`.
* **`remove_sideproducts` / `init-workingDir`**: left to cluster housekeeping (working-directory
  retention) rather than modelled as Airflow steps.
* **`transientfault`**: written by `MetadataExtractorJob` for analysis only and never persisted in
  cache, matching the original behaviour.
* **Cache layout**: the cache is stored via `CacheStorageUtils` using the standard
  `<cache_location>/<cache_id>/{data,fault}` layout (with `meta.json` holding the current cache id)
  instead of the Oozie-specific `<cache_location>/<cache_id>/{meta,fault}` naming. Since the
  metadata extraction cache is (re)created in the k8s environment this is a non-breaking change and
  makes the cache consistent with the other cached IIS jobs.

## Not covered by this migration

The following Oozie workflows referenced by the metadata extraction hierarchy are **not** part of
this change because they are either PMC/XML specific or unrelated PIG-based phases that are
handled elsewhere (they are not listed among the workflows requiring porting):

* `eu/dnetlib/iis/wf/ingest/pmc/prefetched/oozie_app` and `eu/dnetlib/iis/wf/ingest/pmc/metadata/oozie_app`
  (PMC/XML metadata ingest; `importer/plaintext` + `ingest/pmc/metadata` +
  `transformers/ingest/pmc/metadata`) — needed only when `import_mode=xmlpmc`.
* `eu/dnetlib/iis/wf/transformers/metadataextraction/documenttext/oozie_app`.

## Parameter conventions

All Spark tasks share the established conventions of this repository:

* `JAR_*` — per-module uber (shaded) JAR URL; `SPARK_IMAGE` — spark 4 image;
  `HADOOP_USER_NAME` — propagated to driver/executor via `spark.*Env.HADOOP_USER_NAME`.
* `$UNDEFINED$` is the sentinel for an unbounded/undefined value, matching the Oozie convention.
* Cache handling relies on `-cacheRootDir` + `-lockManagerFactoryClassName`
  (`HadoopFsLockManagerFactory` requires no external coordination service; use
  `ZookeeperLockManagerFactory` only for legacy Hadoop HA clusters).
