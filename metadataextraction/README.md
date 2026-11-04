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

## Optional content-url transformations (`input_id_mapping` / `input_id`)

These two parameters are *not* declared by the Oozie `cache/builder` workflow itself — they were
forwarded from the parent (primary) workflow via `<propagate-configuration/>` into the
`import_content_url` sub-workflow, which is consumed by the `importer/content_url/chain` workflow.
In the Airflow port they are declared on the **builder** DAG and forwarded to the
`spark_content_url_chain` DAG through `TriggerDagRunOperator.conf`:

| Parameter | Consumed by | Spark job | Datastore |
|---|---|---|---|
| `input_id_mapping` | `transformers_idreplacer` | `IdReplacerJob` (`-inputIdMapping`) | `eu.dnetlib.iis.common.schemas.IdentifierMapping` |
| `input_id` | `transformers_common_existencefilter` | `ExistenceFilterJob` (`-inputExistentId`) | `eu.dnetlib.iis.common.schemas.Identifier` |

Both default to `$UNDEFINED$`; when left undefined the corresponding step is skipped
(`BranchPythonOperator` decisions in `spark_content_url_chain`), exactly as in Oozie.

To provide them, override the params when triggering the builder DAG, e.g.:

```json
{
  "input_id_mapping": "hdfs://nameservice1/user/dnet.production/iis/working_dirs/primary/id_mapping",
  "input_id": "hdfs://nameservice1/user/dnet.production/iis/working_dirs/primary/existent_id"
}
```

Note the mapping direction: **`input_id_mapping` belongs to the id-replacer** and
**`input_id` belongs to the existence filter** (the description in the original task statement had
them the other way round).

## Building the uber JARs (`JAR_*` params)

Every module referenced by a `JAR_*` param declares a `shade-uber-jar` Maven profile (the same
profile that already existed in `iis-wf-transformers`), producing a `*-uber.jar` attached with the
`uber` classifier:

```bash
cd ~/iis
mvn package -Pshade-uber-jar -Dmaven.test.skip=true \
  -pl iis-wf/iis-wf-import,iis-wf/iis-wf-transformers,iis-wf/iis-wf-metadataextraction,iis-wf/iis-wf-report
```

Artifacts:

| Module | `JAR_*` param | Artifact |
|---|---|---|
| `iis-wf-import` | `JAR_IMPORT` | `iis-wf-import-<version>-uber.jar` |
| `iis-wf-transformers` | `JAR_TRANSFORMERS` | `iis-wf-transformers-<version>-uber.jar` |
| `iis-wf-metadataextraction` | `JAR_METADATAEXTRACTION` | `iis-wf-metadataextraction-<version>-uber.jar` |
| `iis-wf-report` | `JAR_REPORT` | `iis-wf-report-<version>-uber.jar` |

`spark`/`hadoop` dependencies are `provided`, so they are intentionally **not** bundled — the Spark
image supplies them. With `shadedArtifactAttached=true`, `mvn install`/`deploy` publishes the uber
artifact automatically.

The shade profiles also apply the same exclusion filters as `iis-wf-primary` / `iis-wf-referenceextraction`:

* `models/*.json` (AWS SDK v2 service models, ~0.5 GB raw)
* `software.amazon.awssdk:*` (~1.8 GB raw)
* `com.amazonaws:*` (~0.6 GB raw)
* `software.amazon.s3.analyticsaccelerator:*`

AWS SDK v1/v2 are provided at runtime by the `spark:4.1.2` image (built with `-Phadoop-cloud`,
plus `hadoop-aws` in the bundled Hadoop distribution), so they must not be shipped in the uber JAR.
Effect on artifact size:

| Module | Before | After |
|---|---|---|
| `iis-wf-import` | ~1.1 GB | ~50 MB |
| `iis-wf-metadataextraction` | ~1.1 GB | ~85 MB |
| `iis-wf-report` | ~29 MB | ~29 MB (no AWS deps) |

Note: `DocumentContentUrlBasedImporterJob` uses `com.amazonaws` classes directly. This works in the
Spark image, but would fail if the JAR were run outside it (e.g. plain JRE pod without `hadoop-aws`).
