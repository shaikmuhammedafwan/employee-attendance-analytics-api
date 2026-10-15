# REVIEW.md

## Defects Found in the Starter Implementation

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | Punch-out endpoint / datetime handling | Naive and timezone-aware datetime values could be compared, causing a runtime error. | Submit a valid punch-out request after punching in; the request may fail with a naive/aware datetime comparison error. | Normalize the punch-in datetime to UTC before comparing timestamps. |
| 2 | Punch-out endpoint | Punch-out validation must reject a punch-out time that is not after punch-in and enforce the 24-hour limit. | Submit a punch-out timestamp earlier than or equal to punch-in, or more than 24 hours later. | Added checks to reject invalid time ordering and punch-outs outside the permitted 24-hour window. |
| 3 | Admin explain endpoint | MongoDB explain results can contain BSON values that FastAPI cannot serialize directly as JSON. | Call an explain endpoint and receive a serialization error involving a BSON `Timestamp`. | Added an `explain_json` helper using `bson.json_util.dumps` and `json.loads` to convert the result to JSON-compatible data. |
| 4 | Admin explain endpoint — `late_leaderboard` | The explain endpoint did not support the late-leaderboard analytics endpoint. | Request `/admin/explain/late_leaderboard` and receive an unsupported-endpoint response. | Added a corresponding explain branch for the late-leaderboard aggregation. |
| 5 | Admin explain endpoint — `department_trend` | The explain endpoint did not support the department-trend analytics endpoint. | Request `/admin/explain/department_trend` and receive an unsupported-endpoint response. | Added a department-trend explain branch and verified that it returned a successful response. |
| 6 | Employee monthly analytics | The monthly attendance metrics needed to be calculated in MongoDB rather than by iterating over attendance records in Python. | Inspect the implementation and test monthly totals against attendance records; Python-side aggregation does not meet the MongoDB aggregation requirement. | Reworked the calculation using a MongoDB aggregation pipeline. |
| 7 | Department summary analytics | Department-level attendance metrics needed to be computed through MongoDB aggregation, including employee and attendance data. | Inspect the implementation and compare department metrics with the underlying employee and attendance records. | Reworked the summary using an aggregation pipeline with `$lookup` and grouped metrics. |

## Items Reviewed That Were Not Considered Defects

- **Weekend handling in department trends:** Sunday should not count as a working day under the implemented Monday-to-Friday rule. The weekday calculation was updated to exclude weekends.
- **MongoDB index usage:** Explain results for the tested attendance-list and monthly-analytics queries showed index scans rather than collection scans. The tested department-summary and late-leaderboard explain results also reported zero collection scans for their attendance lookups.
- **Explain output serialization:** BSON values in explain results are not inherently defects in MongoDB; the issue was returning them without converting them to JSON-compatible values.

## Testing Limitations

The endpoints and explain responses were tested against the available development dataset. The observed index usage is evidence from those tests only; it does not establish performance or index behavior for a dataset of 100,000 records. A separate large-dataset test is required to verify that requirement.
