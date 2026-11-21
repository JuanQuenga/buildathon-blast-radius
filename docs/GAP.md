# Candidate-2 schedule excess

Times are seconds. The floor relaxes worker packing. Startup is the difference in worker counts from scheduling only failures. Placement measures the remaining difference between the floor and failure loads on their assigned workers. To allocate the remaining excess exactly once, start with failures on those workers, then insert required passing tests longest first, then free riders. The table shows the three largest resulting increases in maximum worker load. Attribution among tests sharing a worker depends on this ordering. The compressed [test ledger](gap-tests.csv.gz) records every selected catalog ID, duration and contribution, including zeros.

| Partition | Patch | Plan | Floor | Startup | Placement | Largest added loads (s) |
|---|---|---:|---:|---:|---:|---|
| sample | 2haz | 25.454 | 0.000 | 4.000 | 0.000 | test_execute_tpcds 21.454 |
| sample | 2jrq | 25.454 | 25.454 | 0.000 | 0.000 |  |
| sample | 2qn5 | 2.536 | 0.000 | 2.000 | 0.000 | test_duckdb 0.287, test_time 0.155, test_week 0.051 |
| sample | 2vdj | 2.083 | 2.083 | 0.000 | 0.000 |  |
| sample | 3utz | 25.454 | 25.454 | 0.000 | 0.000 |  |
| sample | 4d63 | 9.078 | 0.000 | 4.000 | 0.000 | test_annotate_funcs 1.963, test_simplify 0.491, test_optimize 0.347 |
| sample | 535z | 2.855 | 2.596 | 0.000 | 0.000 | test_datetime_functions 0.096, test_oracle 0.057, test_week 0.051 |
| sample | 5y72 | 2.466 | 0.000 | 2.000 | 0.000 | test_optimize 0.395, test_correlated_exists_over_a_nested_scalar_aggregate 0.050, test_eliminate_subqueries 0.021 |
| sample | 66ks | 24.787 | 0.000 | 2.000 | 0.000 | test_execute_tpcds 21.454, test_subqueries 0.186, test_scalar_functions 0.169 |
| sample | 6vvp | 2.037 | 2.037 | 0.000 | 0.000 |  |
| sample | 6ze8 | 2.433 | 2.001 | 0.000 | 0.000 | test_time 0.155, test_is_precedence 0.144, test_partition_by_excluding 0.026 |
| sample | 8y7j | 0.000 | 0.000 | 0.000 | 0.000 |  |
| sample | 986c | 2.340 | 2.329 | 0.000 | 0.000 | test_no_redundant_expression_metadata 0.011 |
| sample | 9u5k | 0.000 | 0.000 | 0.000 | 0.000 |  |
| sample | ary6 | 4.382 | 4.382 | 0.000 | 0.000 |  |
| sample | bap8 | 0.000 | 0.000 | 0.000 | 0.000 |  |
| sample | bj7g | 2.241 | 2.011 | 0.000 | 0.000 | test_clickhouse 0.230 |
| sample | bvw2 | 2.061 | 2.061 | 0.000 | 0.000 |  |
| sample | egys | 2.596 | 2.596 | 0.000 | 0.000 |  |
| sample | f4bn | 2.007 | 2.007 | 0.000 | 0.000 |  |
| sample | f4xa | 0.000 | 0.000 | 0.000 | 0.000 |  |
| sample | fryt | 2.157 | 0.000 | 2.000 | 0.000 | test_time 0.155, test_date_add 0.001, test_date_sub 0.001 |
| sample | gbav | 2.004 | 0.000 | 2.000 | 0.000 | test_show 0.004 |
| sample | gbhk | 2.061 | 2.061 | 0.000 | 0.000 |  |
| sample | j6uz | 4.399 | 4.399 | 0.000 | 0.000 |  |
| sample | ktz3 | 2.078 | 0.000 | 2.000 | 0.000 | test_ddl 0.054, test_postgres_create_trigger 0.016, test_create_trigger 0.008 |
| sample | mthh | 3.016 | 3.016 | 0.000 | 0.000 |  |
| sample | nwnx | 2.092 | 2.092 | 0.000 | 0.000 |  |
| sample | rszg | 5.775 | 5.764 | 0.000 | 0.000 | test_no_redundant_expression_metadata 0.011 |
| sample | st8f | 2.008 | 2.008 | 0.000 | 0.000 |  |
| dev | hjpw | 25.454 | 4.982 | 2.000 | 0.000 | test_execute_tpcds 18.472 |
| exotic | 12gn | 10.577 | 10.577 | 0.000 | 0.000 |  |
| exotic | 18df | 36.817 | 0.000 | 8.000 | 0.000 | test_max_nodes 28.817 |
| exotic | 2e7q | 36.817 | 9.226 | 4.000 | -0.597 | test_max_nodes 24.188 |
| exotic | 31t6 | 0.000 | 0.000 | 0.000 | 0.000 |  |
| exotic | 35ss | 27.160 | 27.160 | 0.000 | 0.000 |  |
| exotic | 3jpn | 0.000 | 0.000 | 0.000 | 0.000 |  |
| exotic | 4c2w | 36.817 | 0.000 | 8.000 | 0.000 | test_max_nodes 28.817 |
| exotic | 4p0b | 2.115 | 0.000 | 2.000 | 0.000 | test_build_removes_obsolete_extensions 0.073, test_dry_run_preserves_obsolete_extensions 0.021, test_editable_install_removes_obsolete_extensions 0.021 |
| exotic | 4zv1 | 2.083 | 2.083 | 0.000 | 0.000 |  |
| exotic | 63ve | 2.115 | 0.000 | 2.000 | 0.000 | test_build_removes_obsolete_extensions 0.073, test_dry_run_preserves_obsolete_extensions 0.021, test_editable_install_removes_obsolete_extensions 0.021 |
| exotic | 6jar | 0.000 | 0.000 | 0.000 | 0.000 |  |
| exotic | 6oz5 | 0.000 | 0.000 | 0.000 | 0.000 |  |
| exotic | 8l9u | 0.000 | 0.000 | 0.000 | 0.000 |  |
| exotic | 8z1z | 0.000 | 0.000 | 0.000 | 0.000 |  |
| exotic | 9cm6 | 27.454 | 8.694 | 4.000 | -0.004 | test_execute_tpcds 14.764 |
| exotic | b6cm | 27.454 | 27.454 | 0.000 | 0.000 |  |
| exotic | c34o | 0.000 | 0.000 | 0.000 | 0.000 |  |
| exotic | csq3 | 0.000 | 0.000 | 0.000 | 0.000 |  |
| exotic | cwb5 | 0.000 | 0.000 | 0.000 | 0.000 |  |
| exotic | d9vy | 11.395 | 11.395 | 0.000 | 0.000 |  |
| exotic | eezd | 0.000 | 0.000 | 0.000 | 0.000 |  |
| exotic | ezwi | 0.000 | 0.000 | 0.000 | 0.000 |  |
| exotic | fiwi | 36.817 | 36.817 | 0.000 | 0.000 |  |
| exotic | frf1 | 0.000 | 0.000 | 0.000 | 0.000 |  |
| exotic | gvgz | 0.000 | 0.000 | 0.000 | 0.000 |  |
| exotic | gzfy | 0.000 | 0.000 | 0.000 | 0.000 |  |
| exotic | h694 | 0.000 | 0.000 | 0.000 | 0.000 |  |
| exotic | jldz | 0.000 | 0.000 | 0.000 | 0.000 |  |
| exotic | kkzn | 27.454 | 8.690 | 4.000 | -0.004 | test_execute_tpcds 14.768 |
| exotic | m16g | 0.000 | 0.000 | 0.000 | 0.000 |  |
| exotic | m33y | 9.249 | 9.249 | 0.000 | 0.000 |  |
| exotic | mim1 | 10.869 | 10.869 | 0.000 | 0.000 |  |
| exotic | mwlk | 36.817 | 0.000 | 8.000 | 0.000 | test_max_nodes 28.817 |
| exotic | nv1m | 27.454 | 10.686 | 2.000 | 0.083 | test_execute_tpcds 14.685 |
| exotic | of5h | 3.406 | 3.406 | 0.000 | 0.000 |  |
| exotic | os6e | 0.000 | 0.000 | 0.000 | 0.000 |  |
| exotic | qbqw | 2.024 | 2.024 | 0.000 | 0.000 |  |
| exotic | rbus | 31.143 | 31.143 | 0.000 | 0.000 |  |
| exotic | rcsj | 0.000 | 0.000 | 0.000 | 0.000 |  |
| exotic | svmf | 3.006 | 3.006 | 0.000 | 0.000 |  |
| exotic | t45s | 0.000 | 0.000 | 0.000 | 0.000 |  |
| exotic | tb0v | 0.000 | 0.000 | 0.000 | 0.000 |  |
| exotic | tgwu | 8.784 | 8.784 | 0.000 | 0.000 |  |
| exotic | ttw5 | 8.784 | 8.784 | 0.000 | 0.000 |  |
| exotic | uk4v | 10.686 | 10.686 | 0.000 | 0.000 |  |
| exotic | uurt | 2.021 | 2.021 | 0.000 | 0.000 |  |
| exotic | wdb7 | 0.000 | 0.000 | 0.000 | 0.000 |  |
| exotic | x6au | 27.160 | 27.160 | 0.000 | 0.000 |  |
| exotic | xd01 | 9.340 | 9.340 | 0.000 | 0.000 |  |
| exotic | xrwf | 8.762 | 8.762 | 0.000 | 0.000 |  |
| revert | 07d3 | 2.033 | 2.033 | 0.000 | 0.000 |  |
| revert | 0uw0 | 27.194 | 2.001 | 2.000 | 0.000 | test_execute_tpcds 21.454, test_ddl 0.213, test_presto 0.072 |
| revert | 0yqz | 25.454 | 0.000 | 4.000 | 0.000 | test_execute_tpcds 21.454 |
| revert | 1jkl | 2.003 | 2.003 | 0.000 | 0.000 |  |
| revert | 1rw0 | 8.784 | 2.002 | 0.000 | 0.000 | test_generator_memory 6.686, test_enum 0.026, test_partition_by_excluding 0.026 |
| revert | 245c | 2.596 | 2.596 | 0.000 | 0.000 |  |
| revert | 2glp | 10.686 | 2.003 | 2.000 | 0.000 | test_generator_memory 6.683 |
| revert | 2img | 36.817 | 2.500 | 6.000 | -0.008 | test_max_nodes 28.325 |
| revert | 2jtu | 4.411 | 2.287 | 0.000 | 0.000 | test_annotate_funcs 1.963, test_spark 0.136, test_time_functions 0.025 |
| revert | 2q32 | 29.019 | 0.000 | 6.000 | 0.000 | test_execute_tpcds 21.454, test_round 0.026, test_ddl 0.022 |
| revert | 3h9v | 9.509 | 2.005 | 2.000 | 0.000 | test_annotate_funcs 1.963, test_simplify 0.496, test_bigquery 0.436 |
| revert | 3jwt | 2.012 | 2.012 | 0.000 | 0.000 |  |
| revert | 3p6e | 28.291 | 2.596 | 4.000 | 0.000 | test_execute_tpcds 20.858, test_json_extract 0.026, test_time 0.011 |
| revert | 3r5r | 4.104 | 2.004 | 0.000 | 0.000 | test_snowflake 0.662, test_serde 0.506, test_identity 0.329 |
| revert | 41x9 | 27.454 | 2.662 | 4.000 | 0.000 | test_execute_tpcds 20.792 |
| revert | 441o | 12.509 | 2.287 | 4.000 | 0.000 | test_scope 2.246, test_optimize_in_nulls 0.491, test_simplify 0.383 |
| revert | 4hwc | 27.454 | 2.136 | 4.000 | 0.000 | test_execute_tpcds 21.318 |
| revert | 4kf2 | 3.647 | 2.011 | 0.000 | 0.000 | test_qualify_columns 1.406, test_clickhouse 0.230 |
| revert | dg8u | 7.870 | 7.790 | 0.000 | 0.080 |  |
| revert | f2wm | 5.296 | 2.596 | 0.000 | 0.000 | test_optimize_in_nulls 1.331, test_optimize_correlated_all 0.686, test_optimize 0.395 |
| revert | ooea | 27.242 | 2.007 | 2.000 | 0.000 | test_execute_tpcds 21.447, test_presto 0.055, test_ilike_semantics 0.055 |
| revert | urr5 | 14.437 | 2.007 | 4.000 | 0.000 | test_generator_memory 6.679, test_json_extract 0.027, test_unnest 0.024 |

## Cost drivers and break-even

Of 71.503 excess seconds on the official sample, 42.908 are TPC-DS on two passing patches, 20.000 are worker fees and 8.595 come from other loads. The 28.817-second parser test contributes zero. All eight fixture edits are already at the floor. Two other sample edits actually fail TPC-DS. Dropping it globally would incur at least 600 penalty seconds. The one extra worker on a 25.454-second TPC-DS plan can carry other plausible tests within its slack. Removing it without reducing the required set can increase the score.

In the labelled development snapshot above the excesses are 20.472 seconds on one mechanical patch, 196.564 seconds on 50 exotic patches and 269.284 seconds on 22 reverts. Full-suite plans include the parser test even when a file is irrelevant, but a change that aborts collection can also make the entire catalog fail. The prior file-only experiment incurred 15 misses on its raw tuning set. The parser test constructs 10,000 columns with depth-100 equality expressions and checks both exception text and limits. A small successful parse does not exercise those assertions. TPC-DS runs every fixture query through spawned executor workers and checks each result. A successful query prefix gives no evidence about the remaining queries. Neither shortcut was adopted.

The operand probe instead observes a specific decision every time it runs on the clean suite, including in spawned workers. The selector recognizes a single identity operator changed to the corresponding equality operator at an observed site, with the remainder of the module structurally identical. Other files in the same patch disable the rule. Observations admit identity-based object equality against None and comparisons between string enum members of the same concrete type using the built-in string operations. Any unsupported operand pair disqualifies the site. Runtime source readers and unknown catalog IDs retain their existing handling.

The two affected official cases save 25.454 and 24.787 seconds. Their mean saving of 25.1205 seconds permits at most an 8.37% added miss probability conditional on triggering, before selector CPU. The batch-wide sample saving is 1.675 seconds per patch, breaking even at 0.558 percentage points of added risk. Observing zero misses in two triggers leaves a one-sided 95% binomial upper limit of 77.6% under independent representative draws. Test order, uninstrumented imports, or different operand types under another environment could make the rule disagree. The input trace and the conditional break-even carry more information here than a pooled zero-miss count. A 40-patch schedule under 160 seconds requires roughly the same fraction of removable identity comparisons as the public sample, a similar true-failure floor, and zero misses.

The frozen candidate recovered 50.241 of the 71.503 seconds, leaving 21.262 seconds above the sample floor. The 73 non-sample tuning cases and 183 aggregate held-out cases had zero triggers. There is no measured non-sample saving per patch and no independent empirical estimate of conditional risk. A proof covering every possible assignment to the observed attributes, or fresh independent labelled cases with actual triggers, would strengthen the case for preferring this rule on finals. Candidate-2 remains the fallback.
