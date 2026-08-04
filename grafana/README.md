# Grafana dashboard

`cronhub-dashboard.json` renders every job CronHub exports on `/metrics` as a
folder-grouped grid of status boxes, coloured by each job's own thresholds.

Built on the [`gapit-htmlgraphics-panel`](https://grafana.com/grafana/plugins/gapit-htmlgraphics-panel/)
plugin, which must be installed before importing.

## Import

Dashboards → New → Import → Upload JSON file.

The dashboard ships with no site-specific values baked in, so after importing:

1. **Point it at your Prometheus.** The queries reference a datasource named
   `Prometheus`. If yours is named something else, pick it in the panel's query
   editor (or rename it in the JSON before importing).
2. **Optionally scope it to one tenant.** By default the queries are unfiltered
   and show every job from every tenant. To narrow it, add a label filter to all
   four queries, e.g. `cronhub_job_value{tenant="my-tenant"}`.

## Queries

| refId | Metric | Used for |
|-------|--------|----------|
| A | `cronhub_job_value` | the number shown in each box |
| B | `cronhub_job_threshold_red` | critical threshold |
| C | `cronhub_job_threshold_yellow` | warning threshold |
| D | `cronhub_job_threshold_direction` | `0` = higher is worse, `1` = lower is worse |

Series are joined by the `job_id` label. Box titles come from the `panelname`
label when a job sets one, falling back to `job_name`; grouping comes from
`folder`.

## Threshold direction

Query D is what lets one panel mix both kinds of job. With `direction=0` a box
goes yellow/red as the value climbs **past** the thresholds; with `direction=1`
it goes yellow/red as the value **drops below** them — for jobs where a low
number is the problem (e.g. warn under 100, critical under 50).

Set the direction per job in CronHub's job form ("Threshold Direction").

If you'd rather not do the comparison in the dashboard at all, CronHub also
exports `cronhub_job_status` (`0` = OK, `1` = WARNING, `2` = CRITICAL), already
evaluated server-side against each job's thresholds and direction. That is the
simpler thing to build new panels on — a single ascending threshold scale
colours it correctly regardless of which direction each job uses.
