# rgwitness

Check Parquet row-group min/max bounds and null counts against the values actually decoded without filters. For supported float storage, also compare present NaN counts. A file can be readable, and have min <= max, yet lose rows when a query engine uses incorrect footer statistics to skip data.

This is an experimental, read-only audit for local files. It does not repair files, run SQL, or claim that a query engine is correct.

## Try a clean file

Python 3.10 or newer, with PyArrow 25.x installed by the package. This creates a
small local fixture; it does not download data or change an existing file.

```sh
python -m pip install "git+https://github.com/anishpentyala/rgwitness.git"
python - <<'PYTHON'
import pyarrow as pa
import pyarrow.parquet as pq
pq.write_table(pa.table({"x": [10, 20, None]}), "sample.parquet")
PYTHON
rgwitness sample.parquet
```

This fixture exits 0 with `CONSISTENT`: its supported footer claims agree with
the unfiltered decoded values. The corrupted-footer example below shows the
failure this audit is intended to catch. Real files can instead return
`INCOMPLETE`, including when claims or types are unsupported. Do not treat that
as a clean result. This is experimental, not a Parquet validity certificate.

A PyPI release is being prepared. Until its package page is linked here, use the
source-install command above.

## Run

```sh
python -m pip install .
rgwitness input.parquet
# or: python -m rgwitness input.parquet
```

Output is one JSON object. Exit codes:

| Code | Verdict | Meaning |
| --- | --- | --- |
| 0 | CONSISTENT | Every supported column/row-group claim checked agrees with the unfiltered scan, with no coverage gaps |
| 1 | CONTRADICTED | At least one recorded bound excludes a decoded value, a recorded null count differs, or another checked claim contradicts the scan |
| 2 | INVALID or output failure | The file could not be admitted, decoded, or handled within the supported layout, or the complete report could not be written |
| 3 | INCOMPLETE | No contradiction found, but some claims or types could not be checked |

The CLI explicitly flushes its report. If stdout is closed, full or otherwise fails, it exits 2 and may produce no complete JSON report. It cannot retract bytes already accepted by a pipe or file. The callable `main(argv)` returns 2 without replacing or closing caller-owned streams; the console/module process runner also prevents a failed buffered write from being retried during interpreter shutdown. Argument-parser usage errors retain argparse behavior. Linux CPython output-failure behavior was tested with a closed pipe and `/dev/full`; no Windows claim is made.

When caller-owned stdout is already closed, the CLI disables argparse color probing only through constructor keywords present in that Python runtime. Open-stream parser defaults are unchanged. This is not an early return: invalid arguments still raise argparse's usage error before a report is attempted. At the closed-output change's introduction, the complete 149-test suite passed on Linux CPython 3.10.12, 3.11.18, 3.12.16 and 3.14.10 with PyArrow 25.0.1, including closed-stream single-file, schema, isolated-worker and batch reports. Nine argparse-only tests also passed on CPython 3.13.18 and 3.15.2 without PyArrow installed. Those narrower checks do not validate decoding or the full tool on 3.13 or 3.15. These are tested configurations, not a general Python or platform compatibility guarantee.

CONSISTENT is limited to these row-group claims. It is not a general Parquet validity certificate, proof of data provenance, or guarantee that filtered queries are safe.

## Example of the failure it detects

A synthetic column contains `[10, 20, null]`, but its footer claims a minimum of 15 and maximum of 20. The file decodes normally. A filter for 10 can skip the entire row group. rgwitness reports:

```json
{
  "verdict": "CONTRADICTED",
  "violation_count": 1,
  "unchecked_count": 0
}
```

The full report identifies row group 0, column `x`, and `lower_excludes_value`, with the claimed and observed bounds. The tests create this example by changing only the footer bytes of a real Parquet fixture, then verify the decoded rows are unchanged.

Large integer bounds use tagged decimal strings in JSON, for example `{"kind":"integer","value":"18446744073709551615"}`. Temporal bounds and observations use exact integer ticks, with a `comparison` object recording the representation and unit (`days`, `ms`, `us` or `ns`). This avoids truncating nanoseconds through Python datetime values and does not depend on pandas. Decimal and binary bounds also have lossless representations. Float report bounds use a tagged `float64` little-endian hex encoding of the Python comparison value, including infinities and signed zero, instead of nonstandard JSON `NaN` or `Infinity`. Float claims also retain their original stored bound bytes as `lower_raw_hex` and `upper_raw_hex`; `comparison.physical_width` distinguishes FLOAT from DOUBLE. A widened Python value is not a claim that decoded float32 NaN payload bits were preserved. Counts remain JSON integers. The Python API `audit_bytes(data)` returns native scalar values, with integer ticks for temporal columns; `rgwitness.report.json_report` prepares the JSON-safe form without changing comparison evidence.

## Input snapshot identity

Every completed audit report includes `input_snapshot.byte_length` and `input_snapshot.sha256`, the lowercase SHA-256 hex digest of the exact byte snapshot passed to the decoder, including its footer. The Python API, ordinary CLI and isolated worker produce the same identity. Renaming a file does not change it. Changing only a footer claim does, even if every decoded value remains the same.

Keep the identity with the report to match it to an input later, for example using `sha256sum input.parquet`. The file at that path might have changed since the audit; the report identifies the bytes read, not a continuing claim about the path. No pathname is added to the report. An INVALID error report has no snapshot identity. The digest is not a signature, a proof of origin, an independent decoder check or a stronger audit verdict.

## Explicit-file batch manifests

Audit a dataset split across several local files without losing a failed member:

```sh
rgwitness --batch part-000.parquet part-001.parquet part-002.parquet
# Apply the worker budget separately to each file:
rgwitness --batch --isolate --timeout 30 part-000.parquet part-001.parquet
```

`--batch` accepts 1 to 32 explicit file arguments. It emits one `rgwitness/batch-v1` JSON object with `file_count`, `verdict_counts` and a `files` list. Each entry has a one-based `position`, the `path` exactly as supplied and the complete ordinary single-file `report`, including snapshot identity when the audit completes. Unlike ordinary single-file reports, a batch manifest **contains input paths**. Review those paths before sharing it.

Files are audited sequentially in argument order. An unreadable or invalid input gets an INVALID entry; later files are still attempted. Duplicate paths are separate reads, not cached or deduplicated, so they can have different snapshot identities if the file changes. There is no directory crawl, sorting, tilde expansion or glob expansion inside rgwitness. Your shell may expand `*.parquet` before invoking it; use quotes for a literal filename. Use `--` before filenames that start with a dash.

The batch verdict and exit code use this precedence, regardless of argument order:

| Condition | Batch verdict | Exit |
| --- | --- | --- |
| Any member is INVALID | INVALID | 2 |
| No INVALID, at least one CONTRADICTED | CONTRADICTED | 1 |
| Neither above, at least one INCOMPLETE | INCOMPLETE | 3 |
| Every member is CONSISTENT | CONSISTENT | 0 |

A contradiction remains in its member report and in `verdict_counts` even when an invalid member makes the overall verdict INVALID. Empty batches, too many arguments and multiple files without `--batch` are usage errors. Output failures still exit 2; bytes already written cannot be retracted. Interrupting a batch before its final output does not emit a partial success manifest.

The Python API `rgwitness.batch.audit_files(paths, isolate=False, timeout=None, memory_mib=None)` returns the same JSON-safe manifest and accepts an iterable of string paths or string-valued path objects. It consumes at most 33 entries before rejecting an over-limit iterable, before opening any input. Invalid API options raise before auditing. Each accepted member retains the existing single-file admission policies. Worker timeout and memory limits apply **per file**, not to the entire batch. All reports are retained in parent memory until output; this does not cap parent memory or report size. The warnings in "Opt-in worker budgets" still apply.

The default batch is a manifest of separate file audits, not an atomic dataset snapshot, a cross-file schema check or a guarantee about a query over the combined dataset. The optional exact schema contract below adds a separate equality check, not a combined-query guarantee. Existing single-file invocation and report shapes are unchanged. Batch behavior was validated on Linux CPython 3.10.12 with PyArrow 25.0.1 only.

## Optional exact schema contract

Require the interpreted schemas of an explicit batch to match:

```sh
rgwitness --batch --require-schema-match part-000.parquet part-001.parquet
rgwitness --batch --require-schema-match --isolate --timeout 30 part-000.parquet part-001.parquet
# Inspect the same descriptor for just one file:
rgwitness --include-schema part-000.parquet
```

This is an opt-in equality contract, not schema inference or evolution compatibility. It compares the decoded Arrow schema's field order, names, logical types and nullability. Integer sign and width, decimal precision/scale/width, time units and timestamp timezones are distinct. Schema and field key/value metadata are excluded and are not copied into the report. The comparison uses structured type descriptions, not equality of formatted type names. It does not equate differently named timezone aliases or decide whether a type conversion is safe.

Each completed member includes a `schema` object with format `rgwitness/schema-v1` and ordered `fields`. Every field contains `name`, `nullable` and a structured `type`. This descriptor and the row-group evidence come from the **same byte snapshot**, with the same `input_snapshot` identity. The path is not reopened to obtain the schema. PyArrow's interpretation, including stored Arrow schema information when applicable, remains part of the evidence. This is not an independent verification of physical schema annotations.

The batch adds `schema_check`:

- `reference_position` is the one-based position of the first completed audit, even if its row-group verdict is CONTRADICTED or INCOMPLETE. It is `null` if none completes.
- `comparisons` records every later completed member's position and its `differences` against that reference. Each difference gives a zero-based `field_position`, a `property` (`name`, `type`, `nullable` or an absent/extra whole `field`), and the `reference` and `actual` values. An empty difference list means exact equality within this contract. Reordered fields are compared by position, not silently aligned by name.
- `unavailable_positions` lists members without completed schema evidence, including invalid inputs and worker timeouts. They are not treated as equal or omitted from coverage.
- The schema verdict is CONTRADICTED if any available schema differs, INCOMPLETE if no difference is found but any member is unavailable, and CONSISTENT otherwise. A single completed member has no peer comparisons; its CONSISTENT schema verdict only means no mismatch was found in that one-member batch. An all-unavailable batch has an INCOMPLETE schema verdict and no reference.

The overall batch verdict uses the same priority across member and schema verdicts: INVALID, then CONTRADICTED, then INCOMPLETE, then CONSISTENT. Thus two individually CONSISTENT files with different schemas yield exit 1. An INVALID member still makes the overall batch INVALID, while an available schema mismatch remains visible in `schema_check`. `verdict_counts` continues to count **file audit verdicts only**, so a schema mismatch can make the overall verdict CONTRADICTED while its CONTRADICTED file count is zero. Existing single-file and batch reports are unchanged unless a new flag is selected.

API equivalents are `audit_bytes(data, include_schema=True)`, `audit_in_worker(path, include_schema=True)` and `audit_files(paths, require_schema_match=True)`. The booleans must be actual `bool` values. `--require-schema-match` requires `--batch`; `--include-schema` is a single-file option. The batch's schema option includes per-member descriptors automatically. Worker containment, admission limits and per-file budgets are unchanged. Each member remains a separate snapshot; schema equality does not prove that the files were captured together, share data provenance, have compatible row contents, or form a correct combined query result. No schema merging, coercion, repair, directory discovery or additional files are read.

At the schema change's introduction, this addition passed the complete suite on Linux CPython 3.10, 3.11 and 3.12 with PyArrow 25.0.1. On CPython 3.14, the 17 new schema tests passed, but the full suite retained a pre-existing closed-stream failure during argparse color detection, reproduced on the unchanged base. No full Python 3.14 compatibility claim is made. Those historical results predate the closed-output parser fix described above. Tests compare 200 seeded schema pairs against Arrow's native structural equality, cover each reported difference category, retain statistics contradictions and gaps separately, verify snapshot replacement/duplicate-path behavior, compare direct and isolated reports, and check unavailable members, timeouts and malformed worker schema output.

## Dictionary string and binary columns

Flat Arrow dictionary columns with decoded `string` or `binary` values are admitted. This includes string categorical columns restored from stored Arrow schema information by PyArrow. It is different from Parquet's physical dictionary encoding, which may also decode to an ordinary scalar Arrow type and was already supported. See Arrow's [dictionary read options](https://arrow.apache.org/docs/python/parquet/parquet_type_handling.html) and the [Parquet encoding definition](https://parquet.apache.org/docs/file-format/data-pages/encodings/).

The scan compares decoded values, not integer category codes or every entry in a dictionary's vocabulary. An unused category is not an extra row. An ordered Arrow dictionary does not replace the Parquet string/binary value order used for the statistics comparison. Missing statistics remain INCOMPLETE; incorrect bounds or null counts can still produce CONTRADICTED.

With `--include-schema` or `--require-schema-match`, a dictionary type has this bounded structure:

```json
{
  "kind": "dictionary",
  "index_type": {"kind": "integer", "bits": 8, "signed": true},
  "value_type": {"kind": "string"},
  "ordered": false
}
```

Index width and sign, value type and the ordered flag are compared exactly. Dictionary contents, category order and code-to-value mappings are **not** part of the Arrow type descriptor or this equality contract. Two files can have equal type descriptors and different categories, values or category mappings. No dictionary values are copied into the schema descriptor. Ordinary bound evidence still contains the claimed and observed extrema. `rgwitness/schema-v1` gains this type kind; older strict consumers may reject these newly admitted reports. Existing admitted scalar descriptors are unchanged.

The descriptor records the decoder's interpreted type, not necessarily the type that existed before writing the file. In PyArrow 25.0.1 round trips tested here, large-string and large-binary dictionary values normalize to ordinary string/binary dictionary values. Numeric dictionaries instead decode as ordinary numeric types and keep their existing scalar audit paths. Other dictionary value types are not newly admitted as dictionary wrappers; nested layouts remain refused. The [Arrow dictionary type](https://arrow.apache.org/docs/python/generated/pyarrow.DictionaryType.html) stores index/value types and the ordered flag, not the dictionary's values.

Existing input and worker limits are unchanged. Declared row/cell limits do not count unused dictionary entries or cap decompression/native memory. This support does not add a security boundary, an independent decoder, a codebook equality check or a category-count audit.

At the dictionary change's introduction, the complete 139-test suite passed on Linux CPython 3.10, 3.11 and 3.12 with PyArrow 25.0.1. The 17 new dictionary tests include 200 seeded categorical files checked against a Python value/extrema oracle, all signed/unsigned 8/16/32/64-bit index widths, ordered flags, unchanged decoded rows after lower/upper/null-count footer corruption, missing statistics, empty/all-null inputs, multiple groups/batches, exact schema differences, and direct/worker/batch snapshot agreement. All 17 passed on CPython 3.14 too, but the full suite retained the same pre-existing argparse closed-stream error as the unchanged base. No full Python 3.14 compatibility claim is made. Those historical results predate the closed-output parser fix described above.

## Unannotated fixed-size binary columns

Flat Arrow `fixed_size_binary` columns, such as fixed-width hash or identifier bytes, are admitted for unsigned-byte comparison only when their raw Parquet schema field has **no converted or logical type annotation** and the column has an explicit `TYPE_ORDER`. The decoded Arrow type alone is not sufficient: a Parquet `INTERVAL` column can decode to `fixed_size_binary[12]` even though its comparison order is undefined. Any converted or logical annotation on an otherwise admitted fixed-byte field leaves an explicit `unsupported_fixed_binary_annotation` gap. It is not compared as unsigned bytes. Some annotations instead produce a different, already unsupported Arrow type and retain the existing layout refusal.

The audit compares unsigned byte sequences, not signed integers, text, UUID string order or host-native integer order. The [Parquet column-order definition](https://github.com/apache/parquet-format/blob/master/src/main/thrift/parquet.thrift) specifies this byte order for unannotated `FIXED_LEN_BYTE_ARRAY` storage. Arrow's [fixed-size binary type](https://arrow.apache.org/docs/python/generated/pyarrow.FixedSizeBinaryType.html) records the byte width.

The scan checks raw modern bounds and null counts against the decoded values. Conservative complete bounds are allowed. A missing bound leaves INCOMPLETE unless another checked claim is contradicted; a present half of a partial pair can still contradict a decoded value. An all-null group with accurate counts and no bounds needs no extrema. Missing or unknown column order leaves an explicit `unsupported_fixed_binary_order` gap rather than assuming an ordering. Raw schema leaf names, physical type and width are aligned with the interpreted flat schema; column paths, bound byte lengths and declared value counts are also checked before raw evidence is compared. A mismatched width or alignment is INVALID, not a consistent result.

The column's `comparison` records `{"representation":"unsigned_bytes","byte_width":16}` for a 16-byte column. Existing tagged-hex JSON encoding preserves bound and observed bytes, including zero and high-bit bytes. With the optional exact schema contract, the type descriptor is `{"kind":"fixed_size_binary","byte_width":16}`. Different fixed widths and variable-width `binary` are distinct, even if their current values happen to fit both. This adds a type kind to `rgwitness/schema-v1`; older strict consumers may reject newly admitted reports. Existing admitted descriptors are unchanged. This descriptor still describes the interpreted Arrow type, not physical annotations. Matching interpreted descriptors do not remove an annotation gap: an INTERVAL member and an unannotated member can have equal fixed-width descriptors while the batch remains INCOMPLETE.

This is not support for Arrow UUID extension types, nested columns or dictionary wrappers whose value type is fixed-size binary. Physical Parquet dictionary encoding of an admitted flat column is separate from those Arrow wrappers. Existing file/row/cell limits still apply. Files with this newly admitted type also use the existing bounded Compact footer reader, including its 1 MiB footer, 100,000-value and depth-32 limits. These limits can reject otherwise readable files. PyArrow still reads initial metadata before these checks. No security boundary, independent data-page decoder, decompression budget, UUID interpretation or checksum verification is added. Worker and batch modes retain their existing caveats.

The complete 171-test suite passed on Linux CPython 3.10.12, 3.11.18, 3.12.16 and 3.14.10 with PyArrow 25.0.1 for this addition. The 22 new tests cover 200 seeded files against an unsigned big-endian integer extrema oracle, widths 1/4/16/32/257, zero/high-bit bytes, multiple groups and decoder batches, missing/partial/contradictory claims, unchanged decoded rows after footer changes, schema width differences, raw alignment/width refusals, the exact independently supplied INTERVAL counterexample, annotation-presence and physical-schema alignment guards, and direct/worker/batch/CLI agreement. Embedded controls were rewritten with Apache Thrift 0.22.0, independently of the production footer reader. Thrift is only needed to regenerate these controls with `python tests/generate_fixed_binary_fixtures.py`, not for ordinary tests or runtime. Other platforms and PyArrow releases are unvalidated.

## What is checked

- Each row group's declared row count against its unfiltered decoded count.
- Present null counts against the decoded null count. A missing count is not treated as zero.
- Present lower and upper bounds against decoded extrema, using the supported scalar type's order.
- Lower <= upper and bounds with no non-null values.
- Conservative bounds are accepted. A stored minimum smaller than the actual minimum is not a corruption by itself.
- Missing min/max with non-null values produces INCOMPLETE. An all-null column need not have min/max.

The reader is PyArrow's `ParquetFile.iter_batches`, with explicit row groups and no predicate. rgwitness does not ask a query engine to recompute min/max, because engines can answer those queries from the same suspect metadata.

## Temporal precision correction

The original implementation could silently report false CONSISTENT for time64 nanosecond bounds within one microsecond when pandas was installed. Without pandas, PyArrow 25.0.1 failed loudly on the original temporal conversion path with INVALID (exit 2), rather than producing that silent false verdict. Temporal comparisons now use raw stored bounds and decoded integer ticks, avoiding that conversion dependency. This correction was validated on Linux CPython with PyArrow 25.0.1 only; no wider runtime or platform compatibility is claimed.

## Float coverage

Flat FLOAT and DOUBLE columns with an explicit `TYPE_ORDER` are compared against unfiltered decoded values. NaNs are counted separately and excluded from extrema. Signed zeros compare equal under the format's compatibility rules, so a stored +0 minimum does not exclude -0, and a stored -0 maximum does not exclude +0. This does not implement IEEE 754 total order.

A present `nan_count` is checked, including an explicit zero. Missing `nan_count` always leaves INCOMPLETE unless another checked claim is contradicted. PyArrow 25.0.1's writer omitted this field in the fixtures tested here, even for NaN-free input. Ordinary files from that writer therefore remain INCOMPLETE; this addition does not silently certify them. Finite bounds that exclude decoded non-NaN values can still be reported as contradictions when the count is missing.

Missing bounds with non-NaN values leave gaps. NaN bounds are ignored and leave an explicit gap, rather than being used in numeric comparisons. An all-NaN or all-null group with accurate counts and no bounds can be CONSISTENT within this scope. Missing/unknown column order, `IEEE_754_TOTAL_ORDER`, and FLOAT16 storage remain unchecked. There is no NaN-payload-order, page-statistics or writer-conformance audit. Bound containment accepts compatible signed zeros even when a writer did not choose the recommended zero sign.

Float metadata comes from a small bounded Compact Thrift footer reader because the installed PyArrow metadata API does not expose NaN counts or column order. Raw evidence is checked against the decoder's group count, column count, physical type, column path and declared number of values before use. The data scan and its interpretation still depend on PyArrow. The reader is not an independent Parquet data-page decoder.

The comparison rules follow the [Parquet format statistics and column-order definitions](https://github.com/apache/parquet-format/blob/master/src/main/thrift/parquet.thrift). Footer encoding follows [Apache Thrift Compact Protocol](https://github.com/apache/thrift/blob/master/doc/specs/thrift-compact-protocol.md).

## Scope and limits

Supported: flat scalar integer, boolean, UTF-8 string, binary, decimal, date, time and timestamp columns supported by the installed PyArrow decoder, the dictionary string/binary coverage above, plus unannotated fixed-size binary with explicit `TYPE_ORDER` and the restricted float coverage above. Temporal comparisons use raw stored bounds and integer-cast decoded ticks. An unexpected temporal physical storage type is unchecked rather than compared in mismatched units. Null-type columns remain unchecked, so a mixed file cannot silently receive CONSISTENT.

Nested layouts, duplicate field names, dotted field names and external column chunks are refused. The dotted-name restriction avoids ambiguity in reader column projection. No page-statistics, column-index, bloom-filter, checksum, encoding, sort-order or schema-evolution audit is claimed. A decoder's interpretation and row boundaries are still part of the evidence, not independently verified format parsing.

Admission policies: one regular local file, at most 64 MiB; at most 2,000,000 declared rows, 128 columns, 4,000,000 declared cells and 4,096 row-group/column pairs. The file is read into a bounded byte snapshot before parsing. Reads use nonblocking open where supported to refuse writerless FIFOs rather than wait on them. These limits are policy, not Parquet format limits. Files with float or fixed-size binary columns also have a 1 MiB footer limit, a 100,000-value Compact parsing budget and a nesting-depth limit of 32; even fields not used for comparison consume that budget. The footer reader refuses malformed fields, duplicate/nonpositive field IDs, overflow, trailing data and unsupported Compact types. These are admission policies, not universal format-validity judgments, and can reject files admitted by earlier releases. PyArrow reads metadata before these extra checks, so the new limits do not bound its initial parsing. This is not a hostile-file sandbox, a decompression budget, a filesystem timeout or a memory/time guarantee. Do not use it on untrusted files without process isolation.

The complete suite passed locally on Linux CPython 3.10, 3.11, 3.12 and 3.14 with PyArrow 25.0.1; the exact versions and narrower parser-only checks are noted above. Other operating systems and PyArrow versions have not been validated. The dependency range is deliberately narrow, not a compatibility claim for every release in it.

## Opt-in worker budgets

The CLI can run the complete input read, native decoder import and audit in a child process:

```sh
rgwitness input.parquet --isolate --timeout 30
# Linux only: also cap the child's virtual address space at 4 GiB
rgwitness input.parquet --isolate --timeout 30 --memory-mib 4096
```

`--isolate` defaults to a 30-second wall-clock timeout. A timeout kills and reaps the direct child, discards any partial output and reports INVALID (exit 2). A crash, failed startup, malformed report or mismatched report/exit status also cannot produce a passing verdict. Complete reports retain the same JSON shape and exit codes as the in-process mode. The Python `audit_bytes` API and default CLI mode are unchanged.

`--memory-mib` applies Linux `RLIMIT_AS` before importing PyArrow. It limits virtual address space, not measured resident memory or a whole process tree. A lower inherited hard limit remains in force. Native libraries can reserve large mappings, so a budget that looks generous for a small file may still prevent imports. A budget failure can yield a generic INVALID error rather than a precise out-of-memory diagnosis. Other platforms reject this option rather than silently ignore it. Timeout values must be finite and positive; both budget options require `--isolate`.

This is process containment for accidental decoder stalls and address-space growth, **not a security sandbox**. The child inherits the caller's permissions, environment, working directory and filesystem/network access. There is no privilege boundary, syscall filter, descendant-process containment or global resource guarantee. The parent captures the child's report in memory; these options do not cap parent memory or all I/O. A parent timeout cannot guarantee interruption of an uninterruptible kernel wait. Do not use this mode as permission to open hostile files. Use an OS/container sandbox with separate resource and access controls for that purpose.

Worker behavior was tested on Linux CPython 3.10.12 with PyArrow 25.0.1: matching valid/contradicted/incomplete/invalid reports, real timed-out and killed children, rejection of partial pass output, generous/low address-space controls, and final stdout failure. No Windows/macOS worker or resource-limit compatibility is claimed.

## Related tools and outside evidence

- [Polars issue 16683](https://github.com/pola-rs/polars/issues/16683) records incorrect UInt64 statistics causing silent row-group pruning. It is evidence of the failure class, not a claim of a current Polars defect.
- [Apache parquet-cli](https://github.com/apache/parquet-java/blob/master/parquet-cli/README.md) has `check-stats`. Its current CheckParquet251Command targets suspect legacy binary-writer page statistics. rgwitness instead checks supported flat scalar row-group claims regardless of writer version. It does not replace the Apache tool's page checks.
- [pq by joewalnes](https://github.com/joewalnes/pq) and [pq by OrlovEvgeny](https://github.com/OrlovEvgeny/pq) offer broader inspection/validation workflows. Their inspected current validation implementations perform metadata sanity/readability checks, not this comparison of each stored bound against unfiltered decoded extrema.
- [parquet-analyzer](https://github.com/clee704/parquet-analyzer) is a deeper structural inspector; its documented validation is footer-only.

These are scope distinctions, not an exhaustive claim of novelty or a performance comparison.

## Development

```sh
python -m unittest discover -s tests -v
```

Tests include real altered-footer bounds/null counts with unchanged decoded data, sub-microsecond time bounds that Python datetime values would truncate, exact temporal unit checks, conservative bounds, missing statistics, all-null inputs, multiple row groups, multiple batches, supported type families, exact JSON large integers, strict exit codes, writerless FIFO refusal on POSIX, and a deterministic 500-case comparison oracle.

Fixed real float controls in `tests/float_cases.py` were generated with PyArrow 25.0.1 and rewritten by an independent Apache Thrift 0.22.0 reader/writer, not the production footer reader. The original Parquet bytes are stored as base64 text constants and decoded in memory at test time, without changing the fixtures. The embedded manifest records expected verdicts and decoded physical-width bits. Tests verify decoded values are unchanged, including valid, narrow/wide bounds, NaN/null counts, all-NaN/all-null groups, infinities, signed zeros, partial bounds and unsupported orders. Fixture maintenance only: with `thrift==0.22.0` installed in a separate development environment, run `python tests/generate_float_fixtures.py`. Apache Thrift is not a runtime or ordinary test dependency.

MIT license. No telemetry or network access is implemented by rgwitness.
